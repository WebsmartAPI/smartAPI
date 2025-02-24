class MetadataRetrievalError(Exception):
    """Custom exception for metadata retrieval failures."""
    
    def __init__(self, status_code, message):
        self.status_code = status_code
        self.message = message
        super().__init__(f"MetadataRetrievalError {status_code}: {message}")

    def to_dict(self):
        """Return error details in JSON-like dictionary format."""
        return {
            "code": self.status_code,
            "success": False,
            "error": "Metadata Retrieval Error",
            "details": str(self)
        }