from library.http import api_http_client, search_api_http_client, general_http_client, requestWrapper
import httpx
from enum import Enum
import PTN
from library.torbox import TORBOX_API_KEY
from library.app import SCAN_METADATA, METADATA_MAX_WORKERS, METADATA_SEARCH_MIN_INTERVAL
from functions.mediaFunctions import constructSeriesTitle, cleanTitle, cleanYear
from functions.databaseFunctions import insertData
import os
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing
import threading
import time

_metadata_search_lock = threading.Lock()
_last_metadata_search_at = 0.0

class DownloadType(Enum):
    torrent = "torrents"
    usenet = "usenet"
    webdl = "webdl"

class IDType(Enum):
    torrents = "torrent_id"
    usenet = "usenet_id"
    webdl = "web_id"

ACCEPTABLE_MIME_TYPES = [
    "video/x-matroska",
    "video/mp4",
]

TYPE_TAG_PREFIX = "type="


def _iter_tag_values(value):
    if value is None:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, nested_value in value.items():
            yield f"{key}={nested_value}"
            yield from _iter_tag_values(nested_value)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_tag_values(item)


def getManualMediaType(item: dict):
    for field in ("tags", "tag", "labels", "label"):
        for tag in _iter_tag_values(item.get(field)) or []:
            normalized_tag = str(tag).strip().lower()
            if normalized_tag.startswith(TYPE_TAG_PREFIX):
                media_type = normalized_tag[len(TYPE_TAG_PREFIX):].strip()
                if media_type in {"movie", "series", "anime"}:
                    return media_type
    return None


def applyManualMediaType(metadata: dict, media_type: str, title_data: dict, file_name: str, item_name: str):
    if media_type not in {"movie", "series", "anime"}:
        return metadata

    metadata["metadata_mediatype"] = media_type
    metadata["metadata_rootfoldername"] = item_name
    metadata["metadata_filename"] = file_name

    if media_type in {"series", "anime"}:
        metadata["metadata_foldername"] = constructSeriesTitle(season=title_data.get("season", 1), folder=True)
        metadata["metadata_season"] = title_data.get("season", 1)
        metadata["metadata_episode"] = title_data.get("episode")

    return metadata

def process_file(item, file, type):
    """Process a single file and return the processed data"""
    if not file.get("mimetype").startswith("video/") or file.get("mimetype") not in ACCEPTABLE_MIME_TYPES:
        logging.debug(f"Skipping file {file.get('short_name')} with mimetype {file.get('mimetype')}")
        return None
    
    data = {
        "item_id": item.get("id"),
        "type": type.value,
        "folder_name": item.get("name"),
        "DEBUG_name": item.get("name"),
        "DEBUG_hash": item.get("hash"),
        "DEBUG_file_name": file.get("short_name"),
        "folder_hash": item.get("hash"),
        "file_id": file.get("id"),
        "file_name": file.get("short_name"),
        "file_size": file.get("size"),
        "file_mimetype": file.get("mimetype"),
        "path": file.get("name"),
        "download_link": f"https://api.torbox.app/v1/api/{type.value}/requestdl?token={TORBOX_API_KEY}&{IDType[type.value].value}={item.get('id')}&file_id={file.get('id')}&redirect=true",
        "extension": os.path.splitext(file.get("short_name"))[-1],              
    }
    title_data = PTN.parse(file.get("short_name"))

    if item.get("name") == item.get("hash"):
        item["name"] = title_data.get("title", file.get("short_name"))

    metadata, _, _ = searchMetadata(title_data.get("title", file.get("short_name")), title_data, file.get("short_name"), f"{item.get('name')} {file.get('short_name')}", item.get("hash"), item.get("name"))
    manual_media_type = getManualMediaType(file) or getManualMediaType(item)
    if manual_media_type:
        logging.info(f"METATRACE manual-type media_type={manual_media_type} item={item.get('name')} file={file.get('short_name')} hash={item.get('hash')}")
        metadata = applyManualMediaType(metadata, manual_media_type, title_data, file.get("short_name"), item.get("name"))
    data.update(metadata)
    logging.debug(data)
    insertData(data, type.value)
    return data

def getUserDownloads(type: DownloadType):
    offset = 0
    limit = 1000

    file_data = []
    
    while True:
        params = {
            "limit": limit,
            "offset": offset,
            "bypass_cache": True,
        }
        try:
            response = api_http_client.get(f"/{type.value}/mylist", params=params)
        except Exception as e:
            logging.error(f"Error fetching {type.value} at offset {offset}: {e}")
            return None, False, f"Error fetching {type.value} at offset {offset}: {e}"
        if response.status_code != 200:
            return None, False, f"Error fetching {type.value} at offset {offset}. {response.status_code}"
        try:
            data = response.json().get("data", [])
        except Exception as e:
            logging.error(f"Error parsing {type.value} at offset {offset}: {e}")
            logging.error(f"Response: {response.text}")
            return None, False, f"Error parsing {type.value} at offset {offset}. {e}"
        if not data:
            break
        file_data.extend(data)
        offset += limit
        if len(data) < limit:
            break

    if not file_data:
        return None, True, f"No {type.value} found."
    
    logging.debug(f"Fetched {len(file_data)} {type.value} items from API.")
    
    files = []
    
    if SCAN_METADATA:
        max_workers = METADATA_MAX_WORKERS
        logging.info(f"Processing files with {max_workers} parallel threads for metadata scanning")
    else:
        max_workers = int(multiprocessing.cpu_count() * 2 - 1)
        logging.info(f"Processing files with {max_workers} parallel threads")
    
    # Collect all files to process
    files_to_process = []
    for item in file_data:
        if not item.get("cached", False):
            continue
        for file in item.get("files", []):
            files_to_process.append((item, file))
    
    # Process files in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_file = {
            executor.submit(process_file, item, file, type): (item, file) 
            for item, file in files_to_process
        }
        
        # Collect results as they complete
        for future in as_completed(future_to_file):
            try:
                data = future.result()
                if data:
                    files.append(data)
            except Exception as e:
                item, file = future_to_file[future]
                logging.error(f"Error processing file {file.get('short_name', 'unknown')}: {e}")
                logging.error(traceback.format_exc())
            
    return files, True, f"{type.value.capitalize()} fetched successfully."

def _throttle_metadata_search():
    global _last_metadata_search_at

    if METADATA_SEARCH_MIN_INTERVAL <= 0:
        return

    with _metadata_search_lock:
        now = time.monotonic()
        wait_time = METADATA_SEARCH_MIN_INTERVAL - (now - _last_metadata_search_at)
        if wait_time > 0:
            time.sleep(wait_time)
        _last_metadata_search_at = time.monotonic()


def searchMetadata(query: str, title_data: dict, file_name: str, full_title: str, hash: str, item_name: str):
    base_metadata = {
        "metadata_title": cleanTitle(query),
        "metadata_link": None,
        "metadata_mediatype": "movie",
        "metadata_image": None,
        "metadata_backdrop": None,
        "metadata_years": None,
        "metadata_season": None,
        "metadata_episode": None,
        "metadata_filename": file_name,
        "metadata_rootfoldername": title_data.get("item_name", None),
    }
    if not SCAN_METADATA:
        base_metadata["metadata_rootfoldername"] = item_name
        return base_metadata, False, "Metadata scanning is disabled."
    extension = os.path.splitext(file_name)[-1]
    metadata_url = f"/meta/search/{full_title}"
    try:
        logging.info(f"METATRACE search-start query={query} file={file_name} hash={hash}")
        _throttle_metadata_search()
        response = requestWrapper(search_api_http_client, "GET", metadata_url, params={"type": "file"})
    except Exception as e:
        logging.error(f"METATRACE search-error query={query} file={file_name} hash={hash} error={e}")
        logging.error(f"Error searching metadata: {e}")
        return base_metadata, False, f"Error searching metadata: {e}. Searching for {query}, item hash: {hash}"
    if response.status_code != 200:
        logging.error(f"METATRACE search-bad-status status={response.status_code} query={query} file={file_name} hash={hash} body={response.text[:500]}")
        logging.error(f"Error searching metadata: {response.status_code}. {response.text}")
        return base_metadata, False, f"Error searching metadata. {response.status_code}. Searching for {query}, item hash: {hash}"
    try:
        results = response.json().get("data", [])
        logging.info(f"METATRACE search-success query={query} file={file_name} hash={hash} results={len(results)}")
        data = results[0]

        title = cleanTitle(data.get("title"))
        base_metadata["metadata_title"] = title
        base_metadata["metadata_years"] = cleanYear(title_data.get("year", None) or data.get("releaseYears", None))

        if data.get("type") == "anime" or data.get("type") == "series":
            series_season_episode = constructSeriesTitle(season=title_data.get("season", None), episode=title_data.get("episode", None))
            file_name = f"{title} {series_season_episode}{extension}"
            base_metadata["metadata_foldername"] = constructSeriesTitle(season=title_data.get("season", 1), folder=True)
            base_metadata["metadata_season"] = title_data.get("season", 1)
            base_metadata["metadata_episode"] = title_data.get("episode")
        elif data.get("type") == "movie":
            file_name = f"{title} ({base_metadata['metadata_years']}){extension}"
        else:
            logging.info(f"METATRACE search-unsupported-type type={data.get('type')} query={query} file={file_name} hash={hash}")
            return base_metadata, False, f"No metadata found. Searching for {query}, item hash: {hash}"
            
        base_metadata["metadata_filename"] = file_name
        base_metadata["metadata_mediatype"] = data.get("type")
        base_metadata["metadata_link"] = data.get("link")
        base_metadata["metadata_image"] = data.get("image")
        base_metadata["metadata_backdrop"] = data.get("backdrop")
        base_metadata["metadata_rootfoldername"] = f"{title} ({base_metadata['metadata_years']})"

        logging.info(f"METATRACE metadata-found type={data.get('type')} title={title} query={query} file={file_name} hash={hash}")
        return base_metadata, True, f"Metadata found. Searching for {query}, item hash: {hash}"
    except IndexError:
        logging.info(f"METATRACE search-empty query={query} file={file_name} hash={hash}")
        return base_metadata, False, f"No metadata found. Searching for {query}, item hash: {hash}"
    except httpx.TimeoutException:
        logging.warning(f"METATRACE search-timeout query={query} file={file_name} hash={hash}")
        return base_metadata, False, f"Timeout searching metadata. Searching for {query}, item hash: {hash}"
    except Exception as e:
        logging.error(f"METATRACE parse-error query={query} file={file_name} hash={hash} error={e}")
        logging.error(f"Error searching metadata: {e}")
        logging.error(f"Error searching metadata: {traceback.format_exc()}")
        return base_metadata, False, f"Error searching metadata: {e}. Searching for {query}, item hash: {hash}"

def getDownloadLink(url: str):
    response = requestWrapper(general_http_client, "GET", url)
    if response.has_redirect_location:
        return response.headers.get('Location')
    return url

def downloadFile(url: str, size: int, offset: int = 0):
    headers = {
        "Range": f"bytes={offset}-{offset + size - 1}",
        **general_http_client.headers,
    }
    response = requestWrapper(general_http_client, "GET", url, use_cache=False, headers=headers)
    if response.status_code == httpx.codes.OK:
        return response.content
    elif response.status_code == httpx.codes.PARTIAL_CONTENT:
        return response.content
    else:
        logging.error(f"Error downloading file: {response.status_code}")
        raise Exception(f"Error downloading file: {response.status_code}")


def streamDownloadFile(url: str, size: int, offset: int = 0, on_chunk=None, chunk_size: int = 256 * 1024):
    headers = {
        "Range": f"bytes={offset}-{offset + size - 1}",
        **general_http_client.headers,
    }

    with general_http_client.stream("GET", url, headers=headers) as response:
        response.raise_for_status()
        if response.status_code not in (httpx.codes.OK, httpx.codes.PARTIAL_CONTENT):
            logging.error(f"Error streaming file: {response.status_code}")
            raise Exception(f"Error streaming file: {response.status_code}")

        received = 0
        for chunk in response.iter_bytes(chunk_size=chunk_size):
            if not chunk:
                continue
            received += len(chunk)
            if on_chunk:
                on_chunk(chunk)
        return received
    
