using System.Buffers.Binary;
using System.Runtime.InteropServices.WindowsRuntime;
using Windows.Media.Core;
using Windows.Media.MediaProperties;

namespace RemoteMac;

internal sealed class VideoFeed : IDisposable
{
    private readonly Connection connection;
    private readonly CancellationTokenSource stop = new();
    private readonly byte[] header = new byte[13];
    private long? origin;
    private long lastTick = -1;

    private VideoFeed(Connection connection, uint width, uint height)
    {
        this.connection = connection;
        var encoding = VideoEncodingProperties.CreateH264();
        encoding.Width = width;
        encoding.Height = height;
        encoding.FrameRate.Numerator = 60;
        encoding.FrameRate.Denominator = 1;
        Source = new MediaStreamSource(new VideoStreamDescriptor(encoding))
        {
            BufferTime = TimeSpan.Zero,
            CanSeek = false,
            IsLive = true
        };
        Source.SampleRequested += OnSampleRequested;
    }

    public MediaStreamSource Source { get; }
    public uint Width { get; private init; }
    public uint Height { get; private init; }

    public static async Task<VideoFeed> StartAsync(Connection connection, CancellationToken token)
    {
        var dimensions = new byte[8];
        await connection.Stream.ReadExactlyAsync(dimensions, token).ConfigureAwait(false);
        uint width = BinaryPrimitives.ReadUInt32BigEndian(dimensions);
        uint height = BinaryPrimitives.ReadUInt32BigEndian(dimensions.AsSpan(4));
        if (width is < 2 or > 4096 || height is < 2 or > 2160)
            throw new IOException("The host returned invalid video dimensions.");
        return new(connection, width, height) { Width = width, Height = height };
    }

    private async void OnSampleRequested(MediaStreamSource sender, MediaStreamSourceSampleRequestedEventArgs args)
    {
        var request = args.Request;
        var deferral = request.GetDeferral();
        try
        {
            await connection.Stream.ReadExactlyAsync(header, stop.Token).ConfigureAwait(false);
            int length = BinaryPrimitives.ReadInt32BigEndian(header);
            if (length is < 1 or > 8_000_000) throw new IOException("Invalid frame size.");
            bool keyFrame = header[4] == 1;
            long pts = BinaryPrimitives.ReadInt64BigEndian(header.AsSpan(5));
            var data = new byte[length];
            await connection.Stream.ReadExactlyAsync(data, stop.Token).ConfigureAwait(false);
            origin ??= pts;
            lastTick = Math.Max(lastTick + 1, (pts - origin.Value) * 10);
            request.Sample = MediaStreamSample.CreateFromBuffer(data.AsBuffer(), TimeSpan.FromTicks(lastTick));
            request.Sample.KeyFrame = keyFrame;
            request.Sample.Duration = TimeSpan.FromTicks(TimeSpan.TicksPerSecond / 60);
        }
        catch (Exception error) when (error is IOException or OperationCanceledException or ObjectDisposedException)
        {
            request.Sample = null;
        }
        finally { deferral.Complete(); }
    }

    public void Dispose()
    {
        Source.SampleRequested -= OnSampleRequested;
        stop.Cancel();
        connection.Dispose();
    }
}
