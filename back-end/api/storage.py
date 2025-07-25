import os
from django.core.files.storage import Storage
from django.core.files.base import ContentFile
from django.conf import settings
from minio import Minio
from minio.error import S3Error
import tempfile
import mimetypes

class MinIOStorage(Storage):
    """
    MinIO storage backend using the official minio Python library.
    This avoids the boto3 compatibility issues with MinIO.
    """
    
    def __init__(self, access_key=None, secret_key=None, bucket_name=None, 
                 endpoint_url=None, location='', **kwargs):
        self.access_key = access_key or os.environ.get('MINIO_ACCESS_KEY')
        self.secret_key = secret_key or os.environ.get('MINIO_SECRET_KEY')
        self.bucket_name = bucket_name or os.environ.get('MINIO_STATIC_BUCKET')
        self.endpoint_url = endpoint_url or os.environ.get('MINIO_URI')
        self.location = location
        
        # Initialize MinIO client
        self.client = Minio(
            self.endpoint_url,
            access_key=self.access_key,
            secret_key=self.secret_key,
            secure=True  # Use HTTPS
        )
    
    def _get_full_path(self, name):
        """Get the full path including location prefix."""
        if self.location:
            return f"{self.location}/{name}"
        return name
    
    def _open(self, name, mode='rb'):
        """Open a file from MinIO."""
        try:
            full_path = self._get_full_path(name)
            response = self.client.get_object(self.bucket_name, full_path)
            return ContentFile(response.read(), name=name)
        except S3Error as e:
            if e.code == 'NoSuchKey':
                raise FileNotFoundError(f"File {name} not found in MinIO")
            raise
    
    def _save(self, name, content):
        """Save a file to MinIO."""
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
        try:
            full_path = self._get_full_path(name)
            self.client.remove_object(self.bucket_name, full_path)
        except S3Error as e:
            if e.code != 'NoSuchKey':
                raise IOError(f"Failed to delete file {name} from MinIO: {e}")
    
    def exists(self, name):
        """Check if a file exists in MinIO."""
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