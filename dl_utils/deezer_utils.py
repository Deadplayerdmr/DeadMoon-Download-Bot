import re

import mutagen


def clean_filename(filename: str) -> str:
    """
    Cleans a string to be suitable for use as a filename or directory name.
    Removes or replaces potentially problematic characters.
    """
    # Replace potentially problematic characters with underscores
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", filename)
    # Remove leading/trailing whitespace and dots
    cleaned = cleaned.strip(" .")
    # Replace consecutive underscores with a single underscore
    cleaned = re.sub(r"_+", "_", cleaned)
    # Ensure the filename is not empty after cleaning
    if not cleaned:
        return "_"
    return cleaned


def get_audio_duration(file_path: str) -> int:
    """Read duration through mutagen for every supported audio container."""
    try:
        audio = mutagen.File(file_path)
        if audio and getattr(audio, "info", None):
            length = getattr(audio.info, "length", None)
            if length:
                return int(length)
        return 0
    except Exception as e:
        print(f"Error getting audio duration for {file_path}: {e}")
        return 0


def download_cover(url: str, timeout: int = 20) -> bytes:
    """Download Deezer artwork at 1200px, then 1000px, then the original URL."""
    import requests

    from dl_utils.metadata_bridge import cover_url_candidates

    candidates = cover_url_candidates(url)
    if not candidates:
        raise ValueError("No cover URL was provided")
    last_error = None
    for candidate in dict.fromkeys(candidates):
        try:
            response = requests.get(candidate, stream=True, timeout=timeout)
            if response.status_code != 200:
                continue
            if response.content:
                return response.content
        except requests.exceptions.RequestException as error:
            last_error = error
    if last_error:
        raise last_error
    raise ValueError(f"Could not download cover artwork from {url}")
