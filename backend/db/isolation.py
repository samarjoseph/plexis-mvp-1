"""
Isolation tag and slug utilities.

Format:
  Account tag: 6 alphanumeric characters [0-9a-z] (mixed letters and digits)
  Chat tag:    8 alphanumeric characters [0-9a-z] (mixed letters and digits)
  Share slug:  <account_tag>-<chat_tag> (e.g. '6a2r32-7b23b2h3')
"""
import hashlib
from typing import Union
from uuid import UUID

CHARSET = "0123456789abcdefghijklmnopqrstuvwxyz"


def compute_account_tag(user_id: Union[str, UUID]) -> str:
    """
    Deterministic 6-character mixed alphanumeric tag for a user account.
    Example: '6a2r32'
    """
    uid_str = str(user_id).strip().lower()
    digest = hashlib.sha256(f"plexis_account_tag_{uid_str}".encode()).digest()
    num = int.from_bytes(digest[:8], byteorder="big")
    chars = []
    for _ in range(6):
        chars.append(CHARSET[num % 36])
        num //= 36
    tag = "".join(chars)
    # Ensure tag contains both letters and digits
    has_alpha = any(c.isalpha() for c in tag)
    has_digit = any(c.isdigit() for c in tag)
    if not (has_alpha and has_digit):
        sub_char = "a" if not has_alpha else "3"
        tag = tag[:-1] + sub_char
    return tag


def compute_chat_tag(chat_id: Union[str, UUID]) -> str:
    """
    Deterministic 8-character mixed alphanumeric tag for a chat.
    Example: '7b23b2h3'
    """
    cid_str = str(chat_id).strip().lower()
    digest = hashlib.sha256(f"plexis_chat_tag_{cid_str}".encode()).digest()
    num = int.from_bytes(digest[:10], byteorder="big")
    chars = []
    for _ in range(8):
        chars.append(CHARSET[num % 36])
        num //= 36
    tag = "".join(chars)
    # Ensure tag contains both letters and digits
    has_alpha = any(c.isalpha() for c in tag)
    has_digit = any(c.isdigit() for c in tag)
    if not (has_alpha and has_digit):
        sub_char = "b" if not has_alpha else "7"
        tag = tag[:-1] + sub_char
    return tag


def generate_chat_slug(user_id: Union[str, UUID], chat_id: Union[str, UUID]) -> str:
    """
    Returns the isolated link format: '<account_tag>-<chat_tag>'
    Example: '6a2r32-7b23b2h3'
    """
    return f"{compute_account_tag(user_id)}-{compute_chat_tag(chat_id)}"
