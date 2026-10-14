// Small native launcher; the game and SDK binaries are packaged unchanged
// except for library search paths and local code signatures.
#import <AppKit/AppKit.h>

#include "package_launcher_ui.inc"

int main() { return runLauncherApp(); }
