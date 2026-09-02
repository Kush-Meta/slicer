"""WKWebView snapshot helper. Run as a subprocess: <html> <png> <w> <h>."""

import sys

from AppKit import NSApplication, NSBitmapImageRep, NSMakeRect
from Foundation import NSDate, NSRunLoop, NSURL
from WebKit import WKSnapshotConfiguration, WKWebView, WKWebViewConfiguration

html_path, out_path, width, height = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])

NSApplication.sharedApplication()
view = WKWebView.alloc().initWithFrame_configuration_(
    NSMakeRect(0, 0, width, height), WKWebViewConfiguration.alloc().init()
)
url = NSURL.fileURLWithPath_(html_path)
view.loadFileURL_allowingReadAccessToURL_(url, url.URLByDeletingLastPathComponent())

loop = NSRunLoop.currentRunLoop()
for _ in range(200):
    loop.runMode_beforeDate_("kCFRunLoopDefaultMode", NSDate.dateWithTimeIntervalSinceNow_(0.05))
    if not view.isLoading():
        break
# a beat for layout and font rendering to settle
for _ in range(6):
    loop.runMode_beforeDate_("kCFRunLoopDefaultMode", NSDate.dateWithTimeIntervalSinceNow_(0.05))

state = {"done": False, "image": None}


def handler(image, error):
    state["image"] = image
    state["done"] = True


view.takeSnapshotWithConfiguration_completionHandler_(
    WKSnapshotConfiguration.alloc().init(), handler
)
deadline = NSDate.dateWithTimeIntervalSinceNow_(15.0)
while not state["done"] and NSDate.date().compare_(deadline) < 0:
    loop.runMode_beforeDate_("kCFRunLoopDefaultMode", NSDate.dateWithTimeIntervalSinceNow_(0.05))

if state["image"] is None:
    print("snapshot failed")
    sys.exit(1)

rep = NSBitmapImageRep.imageRepWithData_(state["image"].TIFFRepresentation())
rep.representationUsingType_properties_(4, {}).writeToFile_atomically_(out_path, True)
print("ok")
