from library.app import RAW_MODE, FUSE_FOREGROUND_SEGMENT_KB, FUSE_PREFETCH_WINDOW_MB, FUSE_PREFETCH_WAIT_MS, FUSE_PREFETCH_MIN_AGE_MS
import os
from library.filesystem import MOUNT_PATH
import stat
import errno
from functions.torboxFunctions import getDownloadLink, downloadFile, streamDownloadFile
import time
import sys
import logging
from functions.appFunctions import getAllUserDownloads
import threading
from sys import platform
from cachetools import LRUCache

# Pull in some spaghetti to make this stuff work without fuse-py being installed
try:
    import _find_fuse_parts # type: ignore # noqa: F401
except ImportError:
    pass
import fuse
from fuse import Fuse
if not hasattr(fuse, '__version__'):
    raise RuntimeError("your fuse-python doesn't know of fuse.__version__, probably it's too old.")

fuse.fuse_python_api = (0, 2)

LINK_AGE = 3 * 60 * 60 # 3 hours

class VirtualFileSystem:
    def __init__(self, files_list):
        self.files = files_list
        self.structure = self._build_structure()
        self.file_map = self._build_file_map()

    def _build_structure(self):
        if RAW_MODE:
            structure = { '/': set() }
            for f in self.files:
                original_path = f.get("path")
                if original_path:
                    # Split the path into parts
                    parts = original_path.split('/')
                    current_path = '/'
                    for part in parts[:-1]:  # Skip the filename
                        if part not in structure.get(current_path, set()):
                            structure.setdefault(current_path, set()).add(part)
                            current_path = f"{current_path}{part}/"
                            structure.setdefault(current_path, set())
            # Ensure consistent ordering
            for key in structure:
                structure[key] = sorted([item for item in structure[key] if item is not None])
            return structure
        else:
            structure = {
                '/': ['movies', 'series'],
                '/movies': set(),
                '/series': set()
            }
        
        
        for f in self.files:
            media_type = f.get('metadata_mediatype')
            root_folder = f.get('metadata_rootfoldername')
            
            if media_type == 'movie':
                path = f'/movies/{root_folder}'
                structure['/movies'].add(root_folder)
                
                if path not in structure:
                    structure[path] = set()
                structure[path].add(f.get('metadata_filename'))
                
            elif media_type == 'series' or media_type == 'anime':
                path = f'/series/{root_folder}'
                structure['/series'].add(root_folder)
                
                if path not in structure:
                    structure[path] = set()
                structure[path].add(f.get('metadata_foldername'))
                
                season_path = f'{path}/{f.get("metadata_foldername")}'
                if season_path not in structure:
                    structure[season_path] = set()
                structure[season_path].add(f.get('metadata_filename'))
        
        # consistent ordering
        for key in structure:
            structure[key] = sorted([item for item in structure[key] if item is not None])
            
        return structure

    def _build_file_map(self):
        file_map = {}
        for f in self.files:
            if RAW_MODE:
                original_path = f.get("path")
                if original_path:
                    path = f'/{original_path}'
                    file_map[path] = f
            else:
                if f.get('metadata_mediatype') == 'movie':
                    path = f'/movies/{f.get("metadata_rootfoldername")}/{f.get("metadata_filename")}'
                    file_map[path] = f
                elif f.get('metadata_mediatype') == 'series' or f.get('metadata_mediatype') == 'anime':
                    path = f'/series/{f.get("metadata_rootfoldername")}/{f.get("metadata_foldername")}/{f.get("metadata_filename")}'
                    file_map[path] = f

        return file_map


    def is_dir(self, path):
        return path in self.structure
        
    def is_file(self, path):
        return path in self.file_map
        
    def get_file(self, path):
        return self.file_map.get(path)
        
    def list_dir(self, path):
        return self.structure.get(path, [])
    
class FuseStat(fuse.Stat):
    def __init__(self):
        self.st_mode = 0
        self.st_ino = 0
        self.st_dev = 0
        self.st_nlink = 0
        self.st_uid = 0
        self.st_gid = 0
        self.st_size = 0
        self.st_atime = 0
        self.st_mtime = 0
        self.st_ctime = 0

class TorBoxMediaCenterFuse(Fuse):
    def __init__(self, *args, **kwargs):
        super(TorBoxMediaCenterFuse, self).__init__(*args, **kwargs)

        self.files = []
        self.vfs = VirtualFileSystem(self.files)
        self.file_handles = {}
        self.next_handle = 1
        self.cached_links = {}

        self.segment_cache = LRUCache(
            maxsize=512 * 1024 * 1024,  # 512MB across all cached ranges
            getsizeof=lambda entry: len(entry['data']),
        )
        self.cache_lock = threading.Lock()
        self.inflight_segments = {}
        self.inflight_prefetch = {}
        self.block_size = 1024 * 1024 * 64  # 64MB logical blocks
        self.segment_size = 1024 * FUSE_FOREGROUND_SEGMENT_KB
        self.prefetch_size = 1024 * 1024 * FUSE_PREFETCH_WINDOW_MB
        self.prefetch_wait_seconds = FUSE_PREFETCH_WAIT_MS / 1000
        self.prefetch_min_age_seconds = FUSE_PREFETCH_MIN_AGE_MS / 1000

        self._refreshFiles()
        threading.Thread(target=self.getFiles, daemon=True).start()

    def _refreshFiles(self):
        files = getAllUserDownloads()
        if files:
            self.files = files
            self.vfs = VirtualFileSystem(self.files)
            logging.info(f"Updated {len(self.files)} files in VFS")
        else:
            logging.warning("No files loaded into VFS yet; keeping previous VFS state")

    def getFiles(self):
        while True:
            self._refreshFiles()
            time.sleep(300)
        
    def getattr(self, path):
        st = FuseStat()
        now = int(time.time())
        st.st_atime = now
        st.st_mtime = now
        st.st_ctime = now
        
        st.st_uid = os.getuid()
        st.st_gid = os.getgid()
        
        if self.vfs.is_dir(path):
            st.st_mode = stat.S_IFDIR | 0o755
            st.st_nlink = 2
            return st
        elif self.vfs.is_file(path):
            file_info = self.vfs.get_file(path)
            if not file_info:
                return -errno.ENOENT
            st.st_mode = stat.S_IFREG | 0o444
            st.st_nlink = 1
            st.st_size = file_info.get('file_size', 0)
            return st
            
        # Not found
        return -errno.ENOENT
    
    def readdir(self, path, _):
        if not self.vfs.is_dir(path):
            return -errno.ENOENT
            
        yield fuse.Direntry('.')
        yield fuse.Direntry('..')
        
        for item in self.vfs.list_dir(path):
            yield fuse.Direntry(item)
    
    def open(self, _, flags):
        accmode = os.O_RDONLY | os.O_WRONLY | os.O_RDWR
        if (flags & accmode) != os.O_RDONLY:
            return -errno.EACCES
    

    def _find_covering_segment(self, path, offset, size):
        if size <= 0:
            return None

        request_end = offset + size - 1
        matched_key = None
        matched_entry = None

        for key, entry in self.segment_cache.items():
            entry_path, _ = key
            if entry_path != path:
                continue
            if entry['start'] <= offset and request_end <= entry['end']:
                matched_key = key
                matched_entry = entry
                break

        if matched_key is None:
            return None

        # Touch the entry so cachetools can maintain LRU ordering.
        matched_entry = self.segment_cache[matched_key]
        matched_entry['last_used'] = time.time()
        return matched_entry

    def _store_segment(self, path, start, data):
        end = start + len(data) - 1
        existing_entry = self._find_covering_segment(path, start, len(data))
        if existing_entry is not None:
            logging.debug(
                f"SEEKTRACE store-skip-covered path={path} offset={start} size={len(data)} covered_start={existing_entry['start']} covered_end={existing_entry['end']}"
            )
            return

        exact_entry = self.segment_cache.get((path, start))
        if exact_entry is not None and exact_entry['end'] >= end:
            exact_entry['last_used'] = time.time()
            logging.debug(
                f"SEEKTRACE store-skip-smaller path={path} offset={start} size={len(data)} existing_end={exact_entry['end']}"
            )
            return

        self.segment_cache[(path, start)] = {
            'start': start,
            'end': end,
            'data': data,
            'last_used': time.time(),
        }

    def _find_covering_inflight_prefetch(self, path, offset, size):
        if size <= 0:
            return None

        request_end = offset + size - 1
        for (entry_path, _), entry in self.inflight_prefetch.items():
            if entry_path != path:
                continue
            if entry['start'] <= offset and request_end <= entry['end']:
                return entry
        return None

    def _read_from_inflight_entry(self, entry, offset, size, timeout=5):
        start_in_segment = offset - entry['start']
        needed = start_in_segment + size
        deadline = time.time() + timeout

        with entry['condition']:
            while len(entry['buffer']) < needed and not entry['done']:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                entry['condition'].wait(timeout=remaining)

            if len(entry['buffer']) >= needed:
                return bytes(entry['buffer'][start_in_segment:needed])

            return None

    def _fetch_segment_stream(self, path, start, fetch_size, download_link, entry, trace_label):
        started_at = time.time()

        def on_chunk(chunk):
            with entry['condition']:
                entry['buffer'].extend(chunk)
                entry['condition'].notify_all()

        try:
            if fetch_size <= 0:
                logging.warning(
                    f"SEEKTRACE {trace_label}-skip path={path} offset={start} fetch_size={fetch_size}"
                )
                return

            received = streamDownloadFile(download_link, fetch_size, start, on_chunk=on_chunk)
            with entry['condition']:
                data = bytes(entry['buffer'])
            if data:
                with self.cache_lock:
                    self._store_segment(path, start, data)
            logging.info(
                f"SEEKTRACE {trace_label}-stream-done path={path} offset={start} fetch_size={fetch_size} received={received} elapsed={time.time() - started_at:.3f}s"
            )
        except Exception as e:
            logging.warning(
                f"SEEKTRACE {trace_label}-stream-error path={path} offset={start} fetch_size={fetch_size} error={e}"
            )
        finally:
            with entry['condition']:
                entry['done'] = True
                entry['condition'].notify_all()
            with self.cache_lock:
                self.inflight_prefetch.pop((path, start), None)
            entry['event'].set()

    def _fetch_segment(self, path, start, fetch_size, download_link, event, trace_label):
        started_at = time.time()
        try:
            if fetch_size <= 0:
                logging.warning(
                    f"SEEKTRACE {trace_label}-skip path={path} offset={start} fetch_size={fetch_size}"
                )
                return
            data = downloadFile(download_link, fetch_size, start)
            if data:
                with self.cache_lock:
                    self._store_segment(path, start, data)
                logging.info(
                    f"SEEKTRACE {trace_label}-done path={path} offset={start} fetch_size={fetch_size} received={len(data)} elapsed={time.time() - started_at:.3f}s"
                )
        except Exception as e:
            logging.warning(
                f"SEEKTRACE {trace_label}-error path={path} offset={start} fetch_size={fetch_size} error={e}"
            )
        finally:
            with self.cache_lock:
                if trace_label == 'prefetch':
                    self.inflight_prefetch.pop((path, start), None)
                else:
                    self.inflight_segments.pop((path, start), None)
            event.set()

    def _ensure_prefetch(self, path, start, fetch_size, download_link, reason='miss'):
        if fetch_size <= 0:
            return None
        with self.cache_lock:
            if self._find_covering_segment(path, start, 1) is not None:
                logging.debug(f"SEEKTRACE prefetch-skip-covered path={path} offset={start} fetch_size={fetch_size} reason={reason}")
                return None

            existing_prefetch = self._find_covering_inflight_prefetch(path, start, 1)
            if existing_prefetch is not None:
                logging.debug(f"SEEKTRACE prefetch-join path={path} offset={start} fetch_size={fetch_size} reason={reason} existing_start={existing_prefetch['start']} existing_end={existing_prefetch['end']}")
                return existing_prefetch

            if (path, start) in self.inflight_segments or (path, start) in self.inflight_prefetch:
                logging.debug(f"SEEKTRACE prefetch-skip-inflight path={path} offset={start} fetch_size={fetch_size} reason={reason}")
                return self.inflight_prefetch.get((path, start))

            event = threading.Event()
            self.inflight_prefetch[(path, start)] = {
                'start': start,
                'end': start + fetch_size - 1,
                'event': event,
                'started_at': time.time(),
                'buffer': bytearray(),
                'condition': threading.Condition(),
                'done': False,
            }
            entry = self.inflight_prefetch[(path, start)]
        logging.info(f"SEEKTRACE prefetch-stream-start path={path} offset={start} fetch_size={fetch_size} reason={reason}")
        threading.Thread(
            target=self._fetch_segment_stream,
            args=(path, start, fetch_size, download_link, entry, 'prefetch'),
            daemon=True,
        ).start()
        return entry

    def _ensure_next_stream(self, path, entry, file_size, download_link):
        next_start = entry['end'] + 1
        if next_start >= file_size:
            return None

        with self.cache_lock:
            inflight_for_path = sum(1 for (entry_path, _), _entry in self.inflight_prefetch.items() if entry_path == path)
        if inflight_for_path >= 2:
            logging.debug(
                f"SEEKTRACE stream-next-skip path={path} offset={next_start} reason=too-many-inflight count={inflight_for_path}"
            )
            return None

        next_block_end = min(
            ((next_start // self.block_size) + 1) * self.block_size - 1,
            file_size - 1,
        )
        next_fetch_size = min(self.prefetch_size, next_block_end - next_start + 1)
        if next_fetch_size <= 0:
            return None

        logging.debug(
            f"SEEKTRACE stream-next path={path} offset={next_start} fetch_size={next_fetch_size} previous_start={entry['start']} previous_end={entry['end']}"
        )
        return self._ensure_prefetch(path, next_start, next_fetch_size, download_link, reason='next-window')

    def _read_from_aligned_segments(self, path, offset, size, file_size, download_link):
        remaining = size
        current_offset = offset
        buffer = bytearray()

        while remaining > 0:
            segment_start = (current_offset // self.segment_size) * self.segment_size
            current_block_end = min(
                ((current_offset // self.block_size) + 1) * self.block_size - 1,
                file_size - 1,
            )
            segment_end = min(segment_start + self.segment_size - 1, current_block_end)
            fetch_size = segment_end - segment_start + 1
            requested_size = min(remaining, segment_end - current_offset + 1)

            if fetch_size <= 0 or requested_size <= 0:
                return None

            with self.cache_lock:
                segment_entry = self._find_covering_segment(path, current_offset, requested_size)
                inflight = self.inflight_segments.get((path, segment_start))
                inflight_prefetch = self._find_covering_inflight_prefetch(path, current_offset, requested_size)

            if segment_entry is None and inflight is not None:
                inflight.wait(timeout=5)
                with self.cache_lock:
                    segment_entry = self._find_covering_segment(path, current_offset, requested_size)

            if segment_entry is None and inflight_prefetch is not None and self.prefetch_wait_seconds > 0:
                prefetch_age = time.time() - inflight_prefetch['started_at']
                if prefetch_age >= self.prefetch_min_age_seconds:
                    logging.info(
                        f"SEEKTRACE wait-prefetch path={path} offset={current_offset} size={requested_size} prefetch_start={inflight_prefetch['start']} prefetch_end={inflight_prefetch['end']} timeout={self.prefetch_wait_seconds:.3f}s age={prefetch_age:.3f}s"
                    )
                    inflight_prefetch['event'].wait(timeout=self.prefetch_wait_seconds)
                    with self.cache_lock:
                        segment_entry = self._find_covering_segment(path, current_offset, requested_size)

            if segment_entry is None:
                if inflight_prefetch is not None:
                    stream_entry = inflight_prefetch
                    logging.info(
                        f"SEEKTRACE stream-join path={path} offset={current_offset} size={remaining} stream_start={stream_entry['start']} stream_end={stream_entry['end']} needed={requested_size}"
                    )
                else:
                    stream_fetch_size = min(self.prefetch_size, current_block_end - segment_start + 1)
                    logging.info(
                        f"SEEKTRACE stream-miss path={path} offset={current_offset} size={remaining} segment_start={segment_start} fetch_size={stream_fetch_size} needed={requested_size}"
                    )
                    stream_entry = self._ensure_prefetch(path, segment_start, stream_fetch_size, download_link, reason='miss')

                if stream_entry is not None:
                    stream_data = self._read_from_inflight_entry(stream_entry, current_offset, requested_size, timeout=5)
                    if stream_data is not None:
                        self._ensure_next_stream(path, stream_entry, file_size, download_link)
                        buffer.extend(stream_data)
                        current_offset += len(stream_data)
                        remaining -= len(stream_data)
                        continue

                with self.cache_lock:
                    segment_entry = self._find_covering_segment(path, current_offset, requested_size)

            if segment_entry is None:
                return None

            logging.debug(
                f"SEEKTRACE stream-cache-hit path={path} offset={current_offset} size={requested_size} segment_start={segment_entry['start']} segment_end={segment_entry['end']}"
            )
            start_in_segment = current_offset - segment_entry['start']
            take = min(remaining, len(segment_entry['data']) - start_in_segment)
            if take <= 0:
                return None
            buffer.extend(segment_entry['data'][start_in_segment:start_in_segment + take])
            current_offset += take
            remaining -= take

        prefetch_start = ((offset + size + self.segment_size - 1) // self.segment_size) * self.segment_size
        if prefetch_start < file_size:
            prefetch_block_end = min(
                ((prefetch_start // self.block_size) + 1) * self.block_size - 1,
                file_size - 1,
            )
            prefetch_fetch_size = min(self.prefetch_size, prefetch_block_end - prefetch_start + 1)
            self._ensure_prefetch(path, prefetch_start, prefetch_fetch_size, download_link, reason='read-ahead')

        return bytes(buffer)

    def read(self, path, size, offset):
        file = self.vfs.get_file(path)

        if not file:
            return -errno.ENOENT

        file_size = file.get('file_size', 0)
        if offset >= file_size:
            return b''

        size = min(size, file_size - offset)
        current_time = time.time()
        if path not in self.cached_links:
            self.cached_links[path] = {
                'link': getDownloadLink(file.get('download_link')),
                'timestamp': current_time
            }
        elif current_time - self.cached_links[path]['timestamp'] > LINK_AGE:
            download_link = getDownloadLink(file.get('download_link'))
            self.cached_links[path] = {
                'link': download_link,
                'timestamp': current_time
            }
        download_link = self.cached_links[path]['link']

        data = self._read_from_aligned_segments(path, offset, size, file_size, download_link)
        if data is None:
            return -errno.EIO
        return data

    def release(self, _, fh):
        if fh in self.file_handles:
            del self.file_handles[fh]
        return 0
    
def runFuse():
    server = TorBoxMediaCenterFuse(
        version="%prog " + fuse.__version__,
        usage="%prog [options] mountpoint",
        dash_s_do="setsingle",
    )

    server.parser.add_option(
        mountopt="root",
        metavar="PATH",
        default=MOUNT_PATH,
        help="Mount point for the filesystem",
    )
    if platform != "darwin":
        server.fuse_args.add(
            "nonempty"
        )
    server.fuse_args.add(
        "allow_other"
    )
    server.fuse_args.add(
        "-f"
    )
    server.parse(values=server, errex=1)
    try:
        server.fuse_args.mountpoint = MOUNT_PATH
    except OSError as e:
        logging.error(f"Error changing directory: {e}")
        sys.exit(1)
    server.main()

def unmountFuse():
    try:
        os.system("fusermount -u " + MOUNT_PATH)
    except OSError as e:
        logging.error(f"Error unmounting: {e}")
        sys.exit(1)
    logging.info("Unmounted successfully.")
