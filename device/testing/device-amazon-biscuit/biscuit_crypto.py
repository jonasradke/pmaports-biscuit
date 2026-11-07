# SPDX-License-Identifier: MIT
"""X25519, ChaCha20-Poly1305 and HKDF for pairing Echos and for the intercom.

Whichever library the running Python has: the voice assistant's venv brings cryptography.
"""
import hashlib
import hmac
import os

try:
    from cryptography.exceptions import InvalidTag as _BadTag
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    def _keypair():
        priv = X25519PrivateKey.generate()
        return (
            priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()),
            priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        )

    def _dh(priv, pub):
        return X25519PrivateKey.from_private_bytes(priv).exchange(
            X25519PublicKey.from_public_bytes(pub))

    def _seal(key, nonce, ad, plain):
        return ChaCha20Poly1305(key).encrypt(nonce, plain, ad)

    def _open(key, nonce, ad, cipher):
        return ChaCha20Poly1305(key).decrypt(nonce, cipher, ad)

except ImportError:
    from nacl.bindings import (
        crypto_aead_chacha20poly1305_ietf_decrypt,
        crypto_aead_chacha20poly1305_ietf_encrypt,
        crypto_scalarmult,
        crypto_scalarmult_base,
    )
    from nacl.exceptions import CryptoError as _BadTag

    def _keypair():
        priv = os.urandom(32)
        return priv, crypto_scalarmult_base(priv)

    def _dh(priv, pub):
        return crypto_scalarmult(priv, pub)

    def _seal(key, nonce, ad, plain):
        return crypto_aead_chacha20poly1305_ietf_encrypt(plain, ad, nonce, key)

    def _open(key, nonce, ad, cipher):
        return crypto_aead_chacha20poly1305_ietf_decrypt(cipher, ad, nonce, key)


def _hkdf(chaining_key, ikm, outputs):
    temp = hmac.new(chaining_key, ikm, hashlib.sha256).digest()
    out, block = [], b""
    for i in range(1, outputs + 1):
        block = hmac.new(temp, block + bytes([i]), hashlib.sha256).digest()
        out.append(block)
    return out
