import os.path
application = defines.get("app", "dist/KaraokePlayer.app")
appname = os.path.basename(application)
files = [application]
symlinks = {"Applications": "/Applications"}
icon = "AppIcon.icns"
icon_locations = {appname: (140, 120), "Applications": (500, 120)}
background = "builtin-arrow"
window_rect = ((100, 100), (640, 280))
