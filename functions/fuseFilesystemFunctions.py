from library.app import RAW_MODE
import os
from library.filesystem import MOUNT_PATH
import stat
import errno
from functions.torboxFunctions import getDownloadLink, downloadFile
import time
import sys
import logging
from functions.appFunctions import getAllUserDownloads
import threading
from sys import platform

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
                
            elif media_type == 'series':
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
                else:  # series
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

        threading.Thread(target=self.getFiles, daemon=True).start()

        self.files = []
        self.vfs = VirtualFileSystem(self.files)
        self.file_handles = {}
        self.next_handle = 1
        self.cached_links = {}

        self.block_cache = {}
        self.slice_cache = {}
        self.cache_lock = threading.Lock()
        self.inflight_prefetch = {}
        self.block_size = 1024 * 1024 * 64  # 64MB Blocks
        self.foreground_window_size = 1024 * 1024  # 1MB foreground read-ahead
        self.max_blocks = 64 # Max 64 blocks in cache (4GB)
        self.max_slice_segments = 128

    def getFiles(self):
        while True:
            files = getAllUserDownloads()
            if files:
                self.files = files
                self.vfs = VirtualFileSystem(self.files)
                logging.debug(f"Updated {len(self.files)} files in VFS")
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
    
    def _trim_caches(self):
        block_limit = self.max_blocks * max(1, len(self.cached_links))
        if len(self.block_cache) > block_limit:
            keys_to_remove = sorted(
                self.block_cache.keys(),
                key=lambda key: self.block_cache[key]['last_used']
            )[:len(self.block_cache) - block_limit]
            for key in keys_to_remove:
                del self.block_cache[key]

        total_segments = sum(len(segments) for segments in self.slice_cache.values())
        slice_limit = self.max_slice_segments * max(1, len(self.cached_links))
        if total_segments > slice_limit:
            all_segments = []
            for path_key, segments in self.slice_cache.items():
                for index, segment in enumerate(segments):
                    all_segments.append((segment['last_used'], path_key, index))
            all_segments.sort(key=lambda item: item[0])
            to_remove = total_segments - slice_limit
            removals = {}
            for _, path_key, index in all_segments[:to_remove]:
                removals.setdefault(path_key, set()).add(index)
            for path_key, indexes in removals.items():
                self.slice_cache[path_key] = [
                    segment for idx, segment in enumerate(self.slice_cache[path_key])
                    if idx not in indexes
                ]
                if not self.slice_cache[path_key]:
                    del self.slice_cache[path_key]

    def _store_block(self, path, block_index, data):
        self.block_cache[(path, block_index)] = {
            'data': data,
            'last_used': time.time(),
        }

    def _get_block(self, path, block_index):
        entry = self.block_cache.get((path, block_index))
        if entry is None:
            return None
        entry['last_used'] = time.time()
        return entry['data']

    def _store_slice(self, path, start, data):
        segment = {
            'start': start,
            'end': start + len(data) - 1,
            'data': data,
            'last_used': time.time(),
        }
        segments = self.slice_cache.setdefault(path, [])
        segments.append(segment)
        segments.sort(key=lambda item: item['start'])

    def _get_slice(self, path, offset, size):
        request_end = offset + size - 1
        for segment in self.slice_cache.get(path, []):
            if segment['start'] <= offset and request_end <= segment['end']:
                segment['last_used'] = time.time()
                start = offset - segment['start']
                end = start + size
                return bytes(segment['data'][start:end])
        return None

    def _prefetch_block(self, path, block_index, download_link, block_offset, block_size, event):
        started_at = time.time()
        try:
            logging.info(
                f"SEEKTRACE prefetch-start path={path} block={block_index} offset={block_offset} size={block_size}"
            )
            data = downloadFile(download_link, block_size, block_offset)
            if data:
                with self.cache_lock:
                    self._store_block(path, block_index, data)
                    self._trim_caches()
                logging.info(
                    f"SEEKTRACE prefetch-done path={path} block={block_index} offset={block_offset} size={block_size} received={len(data)} elapsed={time.time() - started_at:.3f}s"
                )
        except Exception as e:
            logging.warning(
                f"SEEKTRACE prefetch-error path={path} block={block_index} offset={block_offset} size={block_size} error={e}"
            )
        finally:
            with self.cache_lock:
                self.inflight_prefetch.pop((path, block_index), None)
            event.set()

    def _ensure_prefetch(self, path, block_index, download_link, block_offset, block_size):
        with self.cache_lock:
            if (path, block_index) in self.block_cache or (path, block_index) in self.inflight_prefetch:
                return
            event = threading.Event()
            self.inflight_prefetch[(path, block_index)] = event
        threading.Thread(
            target=self._prefetch_block,
            args=(path, block_index, download_link, block_offset, block_size, event),
            daemon=True,
        ).start()

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

        start_block = offset // self.block_size
        end_block = (offset + size - 1) // self.block_size
        buffer = bytearray()

        for block_index in range(start_block, end_block + 1):
            block_offset = block_index * self.block_size
            block_end = min((block_index + 1) * self.block_size - 1, file_size - 1)
            current_block_size = block_end - block_offset + 1
            request_start = max(offset, block_offset)
            request_end = min(offset + size - 1, block_end)
            request_size = request_end - request_start + 1

            with self.cache_lock:
                block_data = self._get_block(path, block_index)
                if block_data is None:
                    slice_data = self._get_slice(path, request_start, request_size)
                else:
                    slice_data = None

            if block_data is not None:
                start_offset_in_block = request_start - block_offset
                end_offset_in_block = start_offset_in_block + request_size
                buffer.extend(block_data[start_offset_in_block:end_offset_in_block])
                continue

            if slice_data is not None:
                buffer.extend(slice_data)
                continue

            fetch_size = min(max(request_size, self.foreground_window_size), block_end - request_start + 1)
            started_at = time.time()
            logging.info(
                f"SEEKTRACE miss path={path} block={block_index} offset={request_start} size={request_size} fetch_size={fetch_size}"
            )
            data = downloadFile(download_link, fetch_size, request_start)
            if not data:
                return -errno.EIO
            with self.cache_lock:
                self._store_slice(path, request_start, data)
                self._trim_caches()
            logging.info(
                f"SEEKTRACE fetch-done path={path} block={block_index} offset={request_start} size={request_size} fetch_size={fetch_size} received={len(data)} elapsed={time.time() - started_at:.3f}s"
            )
            buffer.extend(data[:request_size])
            self._ensure_prefetch(path, block_index, download_link, block_offset, current_block_size)

        return bytes(buffer)
    
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
