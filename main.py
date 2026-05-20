from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.background import BackgroundScheduler
from functions.appFunctions import bootUp, getMountMethod, getAllUserDownloadsFresh, getMountRefreshTime
from functions.databaseFunctions import closeAllDatabases
import logging
import os
from sys import platform


def get_log_level(name: str, default: str) -> int:
    value = os.getenv(name, default).upper()
    return getattr(logging, value, getattr(logging, default.upper(), logging.INFO))


app_log_level = get_log_level("LOG_LEVEL", "INFO")
httpx_log_level = get_log_level("HTTPX_LOG_LEVEL", os.getenv("LOG_LEVEL", "WARNING"))

logging.basicConfig(
    level=app_log_level,
    format='%(asctime)s,%(msecs)03d %(name)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logging.getLogger("httpx").setLevel(httpx_log_level)
logging.getLogger("httpcore").setLevel(httpx_log_level)

if __name__ == "__main__":
    bootUp()
    mount_method = getMountMethod()

    if mount_method == "strm":
        scheduler = BlockingScheduler()
    elif mount_method == "fuse":
        if platform == "win32":
            logging.error("The FUSE mount method is not supported on Windows. Please use the STRM mount method or run this application on a Linux system.")
            exit(1)
        scheduler = BackgroundScheduler()
    else:
        logging.error("Invalid mount method specified.")
        exit(1)

    user_downloads = getAllUserDownloadsFresh()

    scheduler.add_job(
        getAllUserDownloadsFresh,
        "interval",
        hours=getMountRefreshTime(),
        id="get_all_user_downloads_fresh",
    )

    try:
        logging.info("Starting scheduler and mounting...")
        if mount_method == "strm":
            from functions.stremFilesystemFunctions import runStrm
            runStrm()
            scheduler.add_job(
                runStrm,
                "interval",
                minutes=5,
                id="run_strm",
            )
            scheduler.start()
        elif mount_method == "fuse":
            from functions.fuseFilesystemFunctions import runFuse
            scheduler.start()
            runFuse()
    except (KeyboardInterrupt, SystemExit):
        if mount_method == "fuse":
            from functions.fuseFilesystemFunctions import unmountFuse
            unmountFuse()
        elif mount_method == "strm":
            from functions.stremFilesystemFunctions import unmountStrm
            unmountStrm()
        closeAllDatabases()
        exit(0)