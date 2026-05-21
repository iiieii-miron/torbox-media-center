import os
from dotenv import load_dotenv
from enum import Enum

load_dotenv()

SCAN_METADATA = os.getenv("ENABLE_METADATA", "false").lower() == "true"
RAW_MODE = os.getenv("RAW_MODE", "false").lower() == "true"
METADATA_MAX_WORKERS = max(1, int(os.getenv("METADATA_MAX_WORKERS", "2")))
METADATA_SEARCH_MIN_INTERVAL = max(0.0, float(os.getenv("METADATA_SEARCH_MIN_INTERVAL", "0.75")))
FUSE_FOREGROUND_SEGMENT_KB = max(64, int(os.getenv("FUSE_FOREGROUND_SEGMENT_KB", "1024")))
FUSE_PREFETCH_WINDOW_MB = max(1, int(os.getenv("FUSE_PREFETCH_WINDOW_MB", "8")))
FUSE_PREFETCH_WAIT_MS = max(0, int(os.getenv("FUSE_PREFETCH_WAIT_MS", "150")))

class MountRefreshTimes(Enum):
    # times are shown in hours
    slowest = 24 # 24 hours
    very_slow = 12 # 12 hours
    slow = 6 # 6 hours
    normal = 3 # 3 hours
    fast = 2 # 2 hours
    ultra_fast = 1 # 1 hour
    instant = 0.1 # 6 minutes

MOUNT_REFRESH_TIME = os.getenv("MOUNT_REFRESH_TIME", MountRefreshTimes.normal.name)
MOUNT_REFRESH_TIME = MOUNT_REFRESH_TIME.lower()
assert MOUNT_REFRESH_TIME in [e.name for e in MountRefreshTimes], f"Invalid mount refresh time: {MOUNT_REFRESH_TIME}. Valid options are: {[e.name for e in MountRefreshTimes]}"

if MOUNT_REFRESH_TIME == "instant":
    print("!!! Instant mount refresh time may cause rate limiting issues with the API. Use with caution. !!!")

if SCAN_METADATA and RAW_MODE:
    SCAN_METADATA = False
    print("!!! RAW_MODE IS NOT COMPATIBLE WITH METADATA SCANNING. Disabling metadata scanning. !!!")
elif SCAN_METADATA:
    print("!!! Metadata scanning is enabled. This may slow down the processing of files. !!!")

if MOUNT_REFRESH_TIME == "instant" and SCAN_METADATA:
    print("!!! Using instant mount refresh time with metadata scanning may lead to excessive API calls. Falling back to 'fast' refresh time. !!!")
    MOUNT_REFRESH_TIME = MountRefreshTimes.fast.value
else:
    MOUNT_REFRESH_TIME = MountRefreshTimes[MOUNT_REFRESH_TIME].value

def getCurrentVersion():
    return "v2.0.0"