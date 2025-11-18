import os
from django.core.files.storage import Storage, FileSystemStorage
from django.core.files.base import ContentFile
from django.conf import settings
from minio import Minio
from minio.error import S3Error
import tempfile
import mimetypes
from pathlib import Path

class MinIOStorage(Storage):
    """
    MinIO storage backend using the official minio Python library.
    This avoids the boto3 compatibility issues with MinIO.
    """
    
    def __init__(self, access_key=None, secret_key=None, bucket_name=None, 
                 endpoint_url=None, location='', **kwargs):
        self.access_key = access_key or os.environ.get('MINIO_ACCESS_KEY')
        self.secret_key = secret_key or os.environ.get('MINIO_SECRET_KEY')
        # Use bucket_name if provided, otherwise fall back to MINIO_BUCKET (for default storage)
        # or MINIO_STATIC_BUCKET (for static files storage)
        if bucket_name:
            self.bucket_name = bucket_name
        else:
            # Default to MINIO_BUCKET for user files, MINIO_STATIC_BUCKET for static files
            self.bucket_name = os.environ.get('MINIO_BUCKET') or os.environ.get('MINIO_STATIC_BUCKET')
        self.endpoint_url = endpoint_url or os.environ.get('MINIO_URI')
        self.location = location
        
        # Initialize MinIO client only if we have the required credentials
        if self.endpoint_url and self.access_key and self.secret_key:
            # Remove protocol if present (MinIO client expects just hostname:port)
            endpoint = self.endpoint_url.replace('https://', '').replace('http://', '')
            self.client = Minio(
                endpoint,
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=True  # Use HTTPS
            )
        else:
            self.client = None
    
    def _get_full_path(self, name):
        """Get the full path including location prefix."""
        if self.location:
            return f"{self.location}/{name}"
        return name
    
    def _open(self, name, mode='rb'):
        """Open a file from MinIO."""
        if not self.client:
            raise IOError("MinIO client not initialized. Check MINIO_URI, MINIO_ACCESS_KEY, and MINIO_SECRET_KEY environment variables.")
        try:
            full_path = self._get_full_path(name)
            response = self.client.get_object(self.bucket_name, full_path)
            return ContentFile(response.read(), name=name)
        except S3Error as e:
            if e.code == 'NoSuchKey':
                raise FileNotFoundError(f"File {name} not found in MinIO bucket {self.bucket_name}")
            raise
    
    def _save(self, name, content):
        """Save a file to MinIO."""
        if not self.client:
            raise IOError("MinIO client not initialized. Check MINIO_URI, MINIO_ACCESS_KEY, and MINIO_SECRET_KEY environment variables.")
        try:
            full_path = self._get_full_path(name)
            
            # Get content type
            content_type, _ = mimetypes.guess_type(name)
            if not content_type:
                content_type = 'application/octet-stream'
            
            # Save to MinIO
            self.client.put_object(
                self.bucket_name,
                full_path,
                content,
                content.size,
                content_type=content_type
            )
            
            return name
        except S3Error as e:
            raise IOError(f"Failed to save file {name} to MinIO: {e}")
    
    def delete(self, name):
        """Delete a file from MinIO."""
        if not self.client:
            raise IOError("MinIO client not initialized. Check MINIO_URI, MINIO_ACCESS_KEY, and MINIO_SECRET_KEY environment variables.")
        try:
            full_path = self._get_full_path(name)
            self.client.remove_object(self.bucket_name, full_path)
        except S3Error as e:
            if e.code != 'NoSuchKey':
                raise IOError(f"Failed to delete file {name} from MinIO: {e}")
    
    def exists(self, name):
        """Check if a file exists in MinIO."""
        if not self.client:
            return False
        try:
            full_path = self._get_full_path(name)
            self.client.stat_object(self.bucket_name, full_path)
            return True
        except S3Error as e:
            if e.code == 'NoSuchKey':
                return False
            raise
    
    def url(self, name):
        """Get the URL for a file."""
        full_path = self._get_full_path(name)
        return f"https://{self.endpoint_url}/{self.bucket_name}/{full_path}"
    
    def size(self, name):
        """Get the size of a file."""
        if not self.client:
            return 0
        try:
            full_path = self._get_full_path(name)
            stat = self.client.stat_object(self.bucket_name, full_path)
            return stat.size
        except S3Error as e:
            if e.code == 'NoSuchKey':
                return 0
            raise
    
    def get_accessed_time(self, name):
        """Get the last accessed time of a file."""
        if not self.client:
            return None
        try:
            full_path = self._get_full_path(name)
            stat = self.client.stat_object(self.bucket_name, full_path)
            return stat.last_modified
        except S3Error as e:
            if e.code == 'NoSuchKey':
                return None
            raise
    
    def get_created_time(self, name):
        """Get the creation time of a file."""
        return self.get_accessed_time(name)
    
    def get_modified_time(self, name):
        """Get the last modified time of a file."""
        return self.get_accessed_time(name)


class DualStorage(Storage):
    """
    Storage backend that writes to both MinIO and local filesystem (PVC).
    Reads from local filesystem first (faster), falls back to MinIO if not found.
    This provides redundancy and fast local access.
    """
    
    def __init__(self, minio_storage=None, local_storage=None, local_base_path=None, **kwargs):
        """
        Initialize dual storage.
        
        Args:
            minio_storage: MinIOStorage instance or dict with MinIO config (from OPTIONS)
            local_storage: FileSystemStorage instance for local storage
            local_base_path: Base path for local storage (e.g., /app/user_files) (from OPTIONS)
        """
        # Extract minio_storage config from kwargs if passed via OPTIONS
        if 'minio_storage' in kwargs:
            minio_config = kwargs.pop('minio_storage')
            if isinstance(minio_config, dict):
                self.minio_storage = MinIOStorage(**minio_config)
            else:
                self.minio_storage = minio_config
        elif minio_storage is not None:
            if isinstance(minio_storage, dict):
                self.minio_storage = MinIOStorage(**minio_storage)
            else:
                self.minio_storage = minio_storage
        else:
            # Try to create from environment if not provided
            minio_uri = os.environ.get('MINIO_URI')
            minio_access_key = os.environ.get('MINIO_ACCESS_KEY')
            minio_secret_key = os.environ.get('MINIO_SECRET_KEY')
            minio_bucket = os.environ.get('MINIO_USER_FILES_BUCKET') or os.environ.get('MINIO_BUCKET')
            if minio_uri and minio_access_key and minio_secret_key and minio_bucket:
                self.minio_storage = MinIOStorage(
                    access_key=minio_access_key,
                    secret_key=minio_secret_key,
                    bucket_name=minio_bucket,
                    endpoint_url=minio_uri,
                    location=""
                )
            else:
                self.minio_storage = None
        
        # Extract local_base_path from kwargs if passed via OPTIONS
        if 'local_base_path' in kwargs:
            local_base_path = kwargs.pop('local_base_path')
        
        self.local_storage = local_storage or FileSystemStorage(location=local_base_path)
        self.local_base_path = local_base_path or os.path.join(settings.BASE_DIR, 'user_files')
        
        # Ensure local directory exists
        if self.local_base_path:
            Path(self.local_base_path).mkdir(parents=True, exist_ok=True)
    
    def _save(self, name, content):
        """Save file to both MinIO and local filesystem."""
        # Read content into memory first so we can write to both locations
        if hasattr(content, 'read'):
            # If content is a file-like object, read it
            if hasattr(content, 'seek'):
                content.seek(0)
            file_content = content.read()
            if hasattr(content, 'seek'):
                content.seek(0)
        else:
            file_content = content
        
        # Save to local filesystem first (faster)
        from django.core.files.base import ContentFile
        local_content = ContentFile(file_content)
        local_name = self.local_storage.save(name, local_content)
        
        # Also save to MinIO if configured
        if self.minio_storage and self.minio_storage.client:
            try:
                # MinIOStorage._save expects content with .size attribute
                minio_content = ContentFile(file_content)
                if not hasattr(minio_content, 'size'):
                    minio_content.size = len(file_content)
                self.minio_storage._save(name, minio_content)
            except Exception as e:
                # Log error but don't fail - local storage succeeded
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Failed to save {name} to MinIO (local save succeeded): {e}")
        
        return local_name
    
    def _open(self, name, mode='rb'):
        """Open file from local filesystem first, fallback to MinIO."""
        # Try local filesystem first (faster)
        try:
            if self.local_storage.exists(name):
                return self.local_storage._open(name, mode)
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to open {name} from local storage: {e}")
        
        # Fallback to MinIO if local file doesn't exist
        if self.minio_storage and self.minio_storage.client:
            try:
                minio_file = self.minio_storage._open(name, mode)
                # Optionally sync from MinIO to local for future reads
                try:
                    content = minio_file.read()
                    if hasattr(minio_file, 'seek'):
                        minio_file.seek(0)
                    # Save to local for next time
                    local_path = os.path.join(self.local_base_path, name)
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    with open(local_path, 'wb') as f:
                        f.write(content)
                    if hasattr(minio_file, 'seek'):
                        minio_file.seek(0)
                except Exception as sync_error:
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.warning(f"Failed to sync {name} from MinIO to local: {sync_error}")
                return minio_file
            except FileNotFoundError:
                pass
        
        # If both fail, raise the original local storage error
        return self.local_storage._open(name, mode)
    
    def delete(self, name):
        """Delete file from both storages."""
        deleted = False
        # Delete from local
        try:
            if self.local_storage.exists(name):
                self.local_storage.delete(name)
                deleted = True
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to delete {name} from local storage: {e}")
        
        # Delete from MinIO
        if self.minio_storage and self.minio_storage.client:
            try:
                self.minio_storage.delete(name)
                deleted = True
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Failed to delete {name} from MinIO: {e}")
        
        if not deleted:
            raise FileNotFoundError(f"File {name} not found in either storage")
    
    def exists(self, name):
        """Check if file exists in either storage."""
        # Check local first (faster)
        if self.local_storage.exists(name):
            return True
        # Check MinIO
        if self.minio_storage and self.minio_storage.client:
            return self.minio_storage.exists(name)
        return False
    
    def url(self, name):
        """Get URL for file (prefer MinIO URL if available)."""
        if self.minio_storage and self.minio_storage.client:
            return self.minio_storage.url(name)
        return self.local_storage.url(name)
    
    def size(self, name):
        """Get file size from local storage, fallback to MinIO."""
        try:
            if self.local_storage.exists(name):
                return self.local_storage.size(name)
        except Exception:
            pass
        
        if self.minio_storage and self.minio_storage.client:
            return self.minio_storage.size(name)
        return 0
    
    def get_accessed_time(self, name):
        """Get accessed time from local storage, fallback to MinIO."""
        try:
            if self.local_storage.exists(name):
                return self.local_storage.get_accessed_time(name)
        except Exception:
            pass
        
        if self.minio_storage and self.minio_storage.client:
            return self.minio_storage.get_accessed_time(name)
        return None
    
    def get_created_time(self, name):
        """Get created time from local storage, fallback to MinIO."""
        return self.get_accessed_time(name)
    
    def get_modified_time(self, name):
        """Get modified time from local storage, fallback to MinIO."""
        return self.get_accessed_time(name) 