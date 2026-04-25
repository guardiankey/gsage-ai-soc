"""gSage AI — AES-256-GCM Encryption for sensitive DB fields."""

from __future__ import annotations

import base64
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.shared.config.settings import get_settings


class FieldEncryption:
    """AES-256-GCM encryption/decryption for database fields."""

    def __init__(self) -> None:
        """Initialize encryption with key from settings."""
        settings = get_settings()
        # Encryption key must be 32 bytes for AES-256, base64-encoded in settings
        key_bytes = base64.b64decode(settings.encryption_key)
        if len(key_bytes) != 32:
            raise ValueError(
                "ENCRYPTION_KEY must be 32 bytes (base64-encoded). "
                "Generate with: python -c \"import secrets,base64; "
                "print(base64.b64encode(secrets.token_bytes(32)).decode())\""
            )
        self.cipher = AESGCM(key_bytes)

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt plaintext string to bytes.

        Returns:
            Nonce (12 bytes) + ciphertext + tag (16 bytes) as single bytes blob.
        """
        if not plaintext:
            return b""

        nonce = os.urandom(12)  # 96-bit nonce for GCM
        plaintext_bytes = plaintext.encode("utf-8")
        ciphertext = self.cipher.encrypt(nonce, plaintext_bytes, None)
        # Prepend nonce to ciphertext for storage
        return nonce + ciphertext

    def decrypt(self, encrypted_blob: Optional[bytes]) -> str:
        """Decrypt bytes blob to plaintext string.

        Args:
            encrypted_blob: Nonce (12 bytes) + ciphertext + tag (16 bytes).

        Returns:
            Decrypted plaintext string.
        """
        if not encrypted_blob:
            return ""

        # Extract nonce (first 12 bytes)
        nonce = encrypted_blob[:12]
        ciphertext = encrypted_blob[12:]
        plaintext_bytes = self.cipher.decrypt(nonce, ciphertext, None)
        return plaintext_bytes.decode("utf-8")


# Global instance (lazy-loaded)
_encryption_instance: Optional[FieldEncryption] = None


def get_encryption() -> FieldEncryption:
    """Get or create global encryption instance."""
    global _encryption_instance
    if _encryption_instance is None:
        _encryption_instance = FieldEncryption()
    return _encryption_instance
