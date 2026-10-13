import base64
import hashlib
import hmac
import logging
import os
import struct
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

try:
    import pyotp
except ImportError:
    pyotp = None

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    pass


def generate_totp(secret: str, interval: int = 30, digits: int = 6) -> str:
    if not secret:
        raise ConfigurationError("TOTP secret key cannot be empty.")

    clean_secret = secret.strip()

    if pyotp is not None:
        try:
            return pyotp.TOTP(clean_secret, interval=interval, digits=digits).now()
        except Exception as e:
            raise ConfigurationError(f"Failed to generate TOTP via pyotp: {e}") from e

    try:
        padding_needed = (8 - len(clean_secret) % 8) % 8
        padded_secret = clean_secret.upper() + ("=" * padding_needed)
        key = base64.b32decode(padded_secret, casefold=True)
        counter = int(time.time() // interval)
        msg = struct.pack(">Q", counter)
        h = hmac.new(key, msg, hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        code = (struct.unpack(">I", h[offset : offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
        return str(code).zfill(digits)
    except Exception as e:
        raise ConfigurationError(f"Failed to generate TOTP: {e}") from e


@dataclass(frozen=True)
class DhanSettings:
    client_id: str
    access_token: str

    @classmethod
    def from_env(cls) -> "DhanSettings":
        client_id = os.environ.get("DHAN_CLIENT_ID")
        access_token = os.environ.get("DHAN_ACCESS_TOKEN")
        missing = []
        if not client_id:
            missing.append("DHAN_CLIENT_ID")
        if not access_token:
            missing.append("DHAN_ACCESS_TOKEN")
        if missing:
            raise ConfigurationError(f"Missing required Dhan environment variable(s): {', '.join(missing)}")
        return cls(client_id=client_id.strip(), access_token=access_token.strip())


@dataclass(frozen=True)
class AngelOneSettings:
    client_id: str
    api_key: str
    password: str
    totp_secret: str

    @classmethod
    def from_env(cls) -> "AngelOneSettings":
        client_id   = os.environ.get("ANGEL_CLIENT_ID", "")
        api_key     = os.environ.get("ANGEL_API_KEY", "")
        password    = os.environ.get("ANGEL_PASSWORD", "")
        totp_secret = os.environ.get("ANGEL_TOTP_SECRET", "")
        missing = []
        if not client_id:
            missing.append("ANGEL_CLIENT_ID")
        if not api_key:
            missing.append("ANGEL_API_KEY")
        if not password:
            missing.append("ANGEL_PASSWORD")
        if not totp_secret:
            missing.append("ANGEL_TOTP_SECRET")
        if missing:
            raise ConfigurationError(f"Missing required AngelOne environment variable(s): {', '.join(missing)}")
        return cls(
            client_id=client_id.strip(),
            api_key=api_key.strip(),
            password=password.strip(),
            totp_secret=totp_secret.strip(),
        )

    def current_totp(self) -> str:
        """Generate a fresh 6-digit TOTP code from the permanent secret."""
        return generate_totp(self.totp_secret)


@dataclass(frozen=True)
class FyersSettings:
    client_id: str
    secret_key: str
    totp_secret: str
    access_token: str = ""  # pre-obtained session token (alternative to TOTP flow)

    @classmethod
    def from_env(cls) -> "FyersSettings":
        client_id = os.environ.get("FYERS_CLIENT_ID")
        secret_key = os.environ.get("FYERS_SECRET_KEY")
        # Accept either FYERS_TOTP_SECRET (TOTP-based flow) or
        # FYERS_ACCESS_TOKEN (pre-obtained session JWT from Fyers web).
        totp_secret = os.environ.get("FYERS_TOTP_SECRET", "")
        access_token = os.environ.get("FYERS_ACCESS_TOKEN", "")
        missing = []
        if not client_id:
            missing.append("FYERS_CLIENT_ID")
        if not secret_key:
            missing.append("FYERS_SECRET_KEY")
        if not totp_secret and not access_token:
            missing.append("FYERS_TOTP_SECRET or FYERS_ACCESS_TOKEN")
        if missing:
            raise ConfigurationError(f"Missing required Fyers environment variable(s): {', '.join(missing)}")
        return cls(
            client_id=client_id.strip(),
            secret_key=secret_key.strip(),
            totp_secret=totp_secret.strip(),
            access_token=access_token.strip(),
        )

    def get_current_totp(self) -> str:
        return generate_totp(self.totp_secret)


class Settings:
    def __init__(self):
        self._dhan: Optional[DhanSettings] = None
        self._angelone: Optional[AngelOneSettings] = None
        self._fyers: Optional[FyersSettings] = None

    @property
    def dhan(self) -> DhanSettings:
        if self._dhan is None:
            self._dhan = DhanSettings.from_env()
        return self._dhan

    @property
    def angelone(self) -> AngelOneSettings:
        if self._angelone is None:
            self._angelone = AngelOneSettings.from_env()
        return self._angelone

    @property
    def fyers(self) -> FyersSettings:
        if self._fyers is None:
            self._fyers = FyersSettings.from_env()
        return self._fyers

    @property
    def fno_option_watchlist(self) -> List[str]:
        """
        Equity underlyings to spend the throttled option-chain budget on.

        The broker caps option-chain requests, so only a subset of the discovered
        equity universe can be kept fresh enough to trade. This is an operator
        choice, not discovery data — leave it unset to let the loop pick a
        capacity-sized slice of whatever discovery returned.
        """
        from utils.symbol_aliases import canonical_symbols
        raw = os.getenv("FNO_OPTION_WATCHLIST", "")
        # Canonicalised so a rename (TATAMOTORS -> TMPV) does not silently drop a
        # name from the watchlist the operator configured.
        return canonical_symbols(s.strip().upper() for s in raw.split(",") if s.strip())


settings = Settings()
