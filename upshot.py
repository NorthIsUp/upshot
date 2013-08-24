#!/usr/bin/env python
import logging
import os
import time

from AppKit import *
from PyObjCTools import AppHelper

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

import Preferences
from lib import utils
from lib.notifications import Growler
from lib.windows import alert

SCREENSHOT_DIR = utils.get_pref(
    domain='com.apple.screencapture', key='location',
    default=os.path.join(os.environ['HOME'], 'Desktop'))
DROPBOX_DIR = utils.detect_dropbox_folder()
PUBLIC_DIR = os.path.join(DROPBOX_DIR or '', 'Public')
SHARE_DIR = os.path.join(PUBLIC_DIR, 'Screenshots')

HOMEPAGE_URL = 'http://upshot.it'
DROPBOX_PUBLIC_INFO = 'https://www.dropbox.com/help/16'

TIME_THRESHOLD = 15  # How many seconds after creation do we handle a file?

# Set up logging
LOG_LEVEL = logging.DEBUG
logging.basicConfig(level=LOG_LEVEL)
log = logging.getLogger('upshot')

# Local settings
try:
    from settings_local import *
except ImportError:
    pass


class Upshot(NSObject):
    """OS X status bar icon."""
    image_paths = {
        'icon16': 'icon16.png',
        'icon16-off': 'icon16-off.png',
    }
    images = {}
    statusitem = None
    observer = None  # Screenshot directory observer.
    menuitems = {}  # Shortcut to our menuitems.

    def applicationDidFinishLaunching_(self, notification):
        self.build_menu()
        # Go do something useful.
        self.startListening_()

    def build_menu(self):
        """Build the OS X status bar menu."""
        # Create the statusbar item
        statusbar = NSStatusBar.systemStatusBar()
        self.statusitem = statusbar.statusItemWithLength_(NSVariableStatusItemLength)

        # Set statusbar icon and color/grayscale mode.
        for tag, img in self.image_paths.items():
            self.images[tag] = NSImage.alloc().initByReferencingFile_(img)
            self.images[tag].setTemplate_(
                utils.get_pref('iconset') == 'grayscale')
        self.statusitem.setImage_(self.images['icon16'])

        self.statusitem.setHighlightMode_(1)
        self.statusitem.setToolTip_('Upshot Screenshot Sharing')

        # Build menu.
        self.menu = NSMenu.alloc().init()

        m = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'Browse Screenshots', 'openShareDir:', '')
        self.menu.addItem_(m)
        self.menuitems['opensharedir'] = m

        self.menu.addItem_(NSMenuItem.separatorItem())

        m = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'Start Screenshot Sharing', 'startListening:', '')
        m.setHidden_(True)  # Sharing is on by default.
        self.menu.addItem_(m)
        self.menuitems['start'] = m

        m = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'Pause Screenshot Sharing', 'stopListening:', '')
        self.menu.addItem_(m)
        self.menuitems['stop'] = m

        m = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'Preferences...', 'openPreferences:', '')
        self.menu.addItem_(m)
        self.menuitems['preferences'] = m

        self.menu.addItem_(NSMenuItem.separatorItem())

        # m = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        #     'Open UpShot Project Website', 'website:', '')
        # self.menu.addItem_(m)
        # self.menuitems['website'] = m

        m = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'About UpShot', 'about:', '')
        self.menu.addItem_(m)
        self.menuitems['about'] = m

        self.menu.addItem_(NSMenuItem.separatorItem())

        m = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            'Quit UpShot', 'quit:', '')
        self.menu.addItem_(m)
        self.menuitems['quit'] = m

        self.statusitem.setMenu_(self.menu)

    def update_menu(self):
        """Update status bar menu based on app status."""
        if self.statusitem is None:
            return

        # Apply iconset
        self.images['icon16'].setTemplate_(
            utils.get_pref('iconset') == 'grayscale')

        running = (self.observer is not None)
        self.statusitem.setImage_(self.images['icon16' if running else 'icon16-off'])

    def openShareDir_(self, sender=None):
        """Open the share directory in Finder."""
        import webbrowser
        webbrowser.open('http://phabricator.local.disqus.net/file/', new=0, autoraise=True)

    def about_(self, sender=None):
        """Open standard About dialog."""
        app = NSApplication.sharedApplication()
        app.activateIgnoringOtherApps_(True)
        app.orderFrontStandardAboutPanel_(sender)

    def website_(self, sender=None):
        """Open the UpShot homepage in a browser."""
        sw = NSWorkspace.sharedWorkspace()
        sw.openURL_(NSURL.URLWithString_(HOMEPAGE_URL))

    def openPreferences_(self, sender=None):
        Preferences.PreferencesWindowController.showWindow()

    def startListening_(self, sender=None):
        """Start listening for changes to the screenshot dir."""
        event_handler = ScreenshotHandler()
        self.observer = Observer()
        self.observer.schedule(event_handler, path=SCREENSHOT_DIR)
        self.observer.start()
        self.update_menu()
        log.debug('Listening for screen shots to be added to: %s' % (
                  SCREENSHOT_DIR))

        growl = Growler.alloc().init()
        growl.notify('UpShot started', 'and listening for screenshots!')

    def stopListening_(self, sender=None):
        """Stop listening to changes ot the screenshot dir."""
        if self.observer is not None:
            self.observer.stop()
            self.observer.join()
            self.observer = None
            log.debug('Stop listening for screenshots.')

            growl = Growler.alloc().init()
            growl.notify('UpShot paused',
                         'Not listening for screenshots for now!')
        self.update_menu()

    def restart_(self, sender=None):
        self.stopListening_()
        self.startListening_()

    def quit_(self, sender=None):
        """Default quit event."""
        log.debug('Terminating.')
        self.stopListening_()
        NSApplication.sharedApplication().terminate_(sender)


class ScreenshotHandler(FileSystemEventHandler):
    """Handle file creation events in our screenshot dir."""
    def on_created(self, event):
        """File creation, handles screen clips."""
        f = event.src_path
        if isinstance(event, FileCreatedEvent) and not (os.path.basename(f).startswith('.')):
            self.handle_screenshot_candidate(f)

    def on_moved(self, event):
        """
        Catch move event: For full screenshots, OS X creates a temp file,
        then moves it to its final name.
        """
        if not isinstance(event, FileMovedEvent):
            return
        self.handle_screenshot_candidate(event.dest_path)

    def handle_screenshot_candidate(self, f):
        """Given a candidate file, handle it if it's a screenshot."""
        # Do not act on files that are too old (so we don't swallow old files
        # that are not new screenshots).
        if os.path.getctime(f) - time.time() > TIME_THRESHOLD:
            return

        # The file could be anything. Only act if it's a screenshot.
        if not utils.is_screenshot(f):
            return

        utils.get_pref('retinascale') and utils.resampleRetinaImage(f, f)

        with open(f, "rb") as image_file:
            from lib.phabricator import Phabricator

            PHAB = Phabricator()  # This will use your ~/.arcrc file

            import base64
            encoded_string = base64.b64encode(image_file.read())
            result = PHAB.file.upload(data_base64=encoded_string)
            phid = result.response
            result = PHAB.file.info(phid=phid)
            url = result['uri']
            object_name = result['objectName']

        logging.debug('Copying to clipboard.')
        utils.pbcopy(url)

        # Notify user.
        growl = Growler.alloc().init()
        growl.setCallback(self.notify_callback)
        growl.notify(
            'Screenshot shared!',
            'Your URL is: %s\n'
            'PHID is: %s\n'
            'FILE is: %s\n'
            'Click here to view file.' % (url, phid, object_name),
            context=f)

    def notify_callback(self, filepath):
        """
        When growl notification is clicked, open Finder with shared file.
        """
        ws = NSWorkspace.sharedWorkspace()
        ws.activateFileViewerSelectingURLs_(
            NSArray.arrayWithObject_(NSURL.fileURLWithPath_(filepath)))


if __name__ == '__main__':
    # Prepare preferences service.
    Preferences.set_defaults()

    app = NSApplication.sharedApplication()
    delegate = Upshot.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()
