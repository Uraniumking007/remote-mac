using System.Buffers.Binary;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Authentication;
using System.Security.Cryptography;
using System.Text.Json;

namespace RemoteMac;

internal sealed class Connection(TcpClient client, SslStream stream) : IDisposable
{
    public SslStream Stream { get; } = stream;

    public static async Task<Connection> OpenAsync(string host, int port, byte[] pin, byte[] key, CancellationToken cancel)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancel);
        deadline.CancelAfter(TimeSpan.FromSeconds(5));
        var token = deadline.Token;
        var tcp = new TcpClient { NoDelay = true };
        SslStream? tls = null;
        try
        {
            await tcp.ConnectAsync(host, port, token).ConfigureAwait(false);
            tls = new SslStream(tcp.GetStream(), false, (_, certificate, _, _) =>
                certificate is not null && CryptographicOperations.FixedTimeEquals(
                    SHA256.HashData(certificate.GetRawCertData()), pin));
            await tls.AuthenticateAsClientAsync(new SslClientAuthenticationOptions
            {
                TargetHost = host,
                EnabledSslProtocols = SslProtocols.Tls13
            }, token).ConfigureAwait(false);
            var nonce = new byte[32];
            await tls.ReadExactlyAsync(nonce, token).ConfigureAwait(false);
            await tls.WriteAsync(Proof(key, nonce, "remote-mac-v1/viewer"u8), token).ConfigureAwait(false);
            var reply = new byte[32];
            await tls.ReadExactlyAsync(reply, token).ConfigureAwait(false);
            if (!CryptographicOperations.FixedTimeEquals(reply, Proof(key, nonce, "remote-mac-v1/host"u8)))
                throw new AuthenticationException("The Mac rejected the pairing credential.");
            return new(tcp, tls);
        }
        catch
        {
            tls?.Dispose();
            tcp.Dispose();
            throw;
        }
    }

    private static byte[] Proof(byte[] key, byte[] nonce, ReadOnlySpan<byte> context)
    {
        var input = new byte[context.Length + nonce.Length];
        context.CopyTo(input);
        nonce.CopyTo(input.AsSpan(context.Length));
        return HMACSHA256.HashData(key, input);
    }

    public async Task SendAsync(object message, CancellationToken token)
    {
        var payload = JsonSerializer.SerializeToUtf8Bytes(message);
        var packet = new byte[payload.Length + 4];
        BinaryPrimitives.WriteInt32BigEndian(packet, payload.Length);
        payload.CopyTo(packet, 4);
        await Stream.WriteAsync(packet, token).ConfigureAwait(false);
    }

    public async Task<JsonDocument> ReceiveAsync(CancellationToken token)
    {
        var header = new byte[4];
        await Stream.ReadExactlyAsync(header, token).ConfigureAwait(false);
        int length = BinaryPrimitives.ReadInt32BigEndian(header);
        if (length is < 1 or > 800_000) throw new IOException("Invalid response size.");
        var payload = new byte[length];
        await Stream.ReadExactlyAsync(payload, token).ConfigureAwait(false);
        return JsonDocument.Parse(payload);
    }

    public void Dispose()
    {
        Stream.Dispose();
        client.Dispose();
    }
}
