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

        self.cache = {}
        self.cache_lock = threading.Lock()
        self.read_state = {}
        self.min_window_size = 1024 * 1024  # 1MB
        self.max_window_size = 1024 * 1024 * 64  # 64MB
        self.window_growth_factor = 2
        self.seek_tolerance = 1024 * 512  # 512KB
        self.state_ttl = 10
        self.max_segments = 64

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
    
    def _get_cached_segment(self, path, offset, size):
        request_end = offset + size - 1
        with self.cache_lock:
            segments = self.cache.get(path, [])
            for segment in segments:
                if segment['start'] <= offset and request_end <= segment['end']:
                    segment['last_used'] = time.time()
                    return segment
        return None

    def _trim_cache(self):
        total_segments = sum(len(segments) for segments in self.cache.values())
        limit = self.max_segments * max(1, len(self.cached_links))
        if total_segments <= limit:
            return

        all_segments = []
        for path, segments in self.cache.items():
            for index, segment in enumerate(segments):
                all_segments.append((segment['last_used'], path, index))
        all_segments.sort(key=lambda item: item[0])

        to_remove = total_segments - limit
        for _, path, index in all_segments[:to_remove]:
            if path in self.cache and index < len(self.cache[path]):
                self.cache[path][index] = None
        for path in list(self.cache.keys()):
            self.cache[path] = [segment for segment in self.cache[path] if segment is not None]
            if not self.cache[path]:
                del self.cache[path]

    def _store_cached_segment(self, path, start, data):
        end = start + len(data) - 1
        segment = {
            'start': start,
            'end': end,
            'data': data,
            'last_used': time.time(),
        }
        with self.cache_lock:
            segments = self.cache.setdefault(path, [])
            segments = [existing for existing in segments if not (existing['start'] >= start and existing['end'] <= end)]
            segments.append(segment)
            segments.sort(key=lambda item: item['start'])
            self.cache[path] = segments
            self._trim_cache()
        return segment

    def _get_next_window_size(self, path, offset, size):
        now = time.time()
        state = self.read_state.get(path)
        sequential = False
        previous_window = self.min_window_size

        if state and now - state['last_read_ts'] <= self.state_ttl:
            previous_window = state.get('window_size', self.min_window_size)
            expected_next = state.get('last_end', 0)
            if abs(offset - expected_next) <= self.seek_tolerance:
                sequential = True

        if sequential:
            window_size = min(previous_window * self.window_growth_factor, self.max_window_size)
        else:
            window_size = self.min_window_size

        self.read_state[path] = {
            'last_offset': offset,
            'last_size': size,
            'last_end': offset + size,
            'last_read_ts': now,
            'window_size': window_size,
        }
        return sequential, window_size

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

        cached_segment = self._get_cached_segment(path, offset, size)
        sequential, window_size = self._get_next_window_size(path, offset, size)
        if cached_segment is not None:
            start = offset - cached_segment['start']
            end = start + size
            return bytes(cached_segment['data'][start:end])

        fetch_size = min(max(size, window_size), file_size - offset)
        started_at = time.time()
        logging.info(
            f"SEEKTRACE miss path={path} offset={offset} size={size} sequential={sequential} window_size={window_size} fetch_size={fetch_size}"
        )
        data = downloadFile(download_link, fetch_size, offset)
        if not data:
            return -errno.EIO
        segment = self._store_cached_segment(path, offset, data)
        logging.info(
            f"SEEKTRACE fetch-done path={path} offset={offset} size={size} fetch_size={fetch_size} received={len(data)} elapsed={time.time() - started_at:.3f}s"
        )
        start = offset - segment['start']
        end = start + size
        return bytes(segment['data'][start:end])
    
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
