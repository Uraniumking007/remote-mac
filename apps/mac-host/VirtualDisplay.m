#import "VirtualDisplay.h"
#import <Foundation/Foundation.h>

@interface CGVirtualDisplayDescriptor : NSObject
@property(copy) NSString *name;
@property dispatch_queue_t queue;
@property unsigned int maxPixelsWide, maxPixelsHigh, vendorID, productID, serialNum;
@property CGSize sizeInMillimeters;
@end
@interface CGVirtualDisplayMode : NSObject
- (instancetype)initWithWidth:(unsigned int)width height:(unsigned int)height refreshRate:(double)rate;
@end
@interface CGVirtualDisplaySettings : NSObject
@property(copy) NSArray *modes;
@end
@interface CGVirtualDisplay : NSObject
- (instancetype)initWithDescriptor:(CGVirtualDisplayDescriptor *)descriptor;
- (BOOL)applySettings:(CGVirtualDisplaySettings *)settings;
@property(readonly) CGDirectDisplayID displayID;
@end

static CGVirtualDisplay *display;
static CGDirectDisplayID originalMain;

static BOOL makeMain(CGDirectDisplayID target, CGDirectDisplayID previous) {
    if (CGMainDisplayID() == target) return YES;
    CGDisplayConfigRef config;
    if (CGBeginDisplayConfiguration(&config) != kCGErrorSuccess) return NO;
    int offset = (int)CGDisplayBounds(target).size.width;
    if (CGConfigureDisplayOrigin(config, target, 0, 0) != kCGErrorSuccess ||
        (previous != target && CGDisplayIsActive(previous) &&
         CGConfigureDisplayOrigin(config, previous, offset, 0) != kCGErrorSuccess)) {
        CGCancelDisplayConfiguration(config);
        return NO;
    }
    return CGCompleteDisplayConfiguration(config, kCGConfigureForSession) == kCGErrorSuccess &&
           CGMainDisplayID() == target;
}

void RemoteMacRemoveDisplay(void) {
    if (display && originalMain && CGDisplayIsActive(originalMain)) {
        makeMain(originalMain, display.displayID);
    }
    display = nil;
    originalMain = 0;
}

CGDirectDisplayID RemoteMacSetDisplay(const char *mode) {
    if (strcmp(mode, "mirror") == 0) {
        RemoteMacRemoveDisplay();
        CGDirectDisplayID ids[16];
        uint32_t count = 0;
        if (CGGetActiveDisplayList(16, ids, &count) == kCGErrorSuccess) {
            for (uint32_t index = 0; index < count; ++index) {
                if (CGDisplayIsBuiltin(ids[index])) return ids[index];
            }
        }
        return CGMainDisplayID();
    }
    if (!display) {
        originalMain = CGMainDisplayID();
        CGVirtualDisplayDescriptor *descriptor = [CGVirtualDisplayDescriptor new];
        descriptor.name = @"Remote Mac 1080p";
        descriptor.queue = dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0);
        descriptor.maxPixelsWide = 1920;
        descriptor.maxPixelsHigh = 1080;
        descriptor.sizeInMillimeters = CGSizeMake(480, 270);
        descriptor.vendorID = 505;
        descriptor.productID = 198;
        descriptor.serialNum = 13903;
        CGVirtualDisplay *candidate = [[CGVirtualDisplay alloc] initWithDescriptor:descriptor];
        CGVirtualDisplaySettings *settings = [CGVirtualDisplaySettings new];
        settings.modes = @[[[CGVirtualDisplayMode alloc] initWithWidth:1920 height:1080 refreshRate:60]];
        if (![candidate applySettings:settings]) return 0;
        const NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:2];
        while (!CGDisplayIsActive(candidate.displayID) && deadline.timeIntervalSinceNow > 0) {
            [NSThread sleepForTimeInterval:0.025];
        }
        if (!CGDisplayIsActive(candidate.displayID)) return 0;
        display = candidate;
    }
    if (strcmp(mode, "primary") == 0 && !makeMain(display.displayID, originalMain)) return 0;
    if (strcmp(mode, "extend") == 0 && CGDisplayIsActive(originalMain) &&
        !makeMain(originalMain, display.displayID)) return 0;
    return display.displayID;
}
