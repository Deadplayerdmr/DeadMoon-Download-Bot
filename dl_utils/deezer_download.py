# From https://github.com/kmille/deezer-downloader/blob/master/deezer_downloader/deezer.py
# MIT License

from __future__ import annotations

import os
import re
import json
import urllib.parse
import html.parser
import requests
from binascii import a2b_hex, b2a_hex
from typing import Any

from Crypto.Hash import MD5
from Crypto.Cipher import Blowfish

# BEGIN TYPES
TYPE_TRACK = "track"
TYPE_ALBUM = "album"
TYPE_PLAYLIST = "playlist"
TYPE_ALBUM_TRACK = "album_track"  # used for listing songs of an album
# END TYPES

session = None
license_token = {}
sound_format = ""
# Metadata of the album most recently fetched by
# get_song_infos_from_deezer_website(); kept as a fallback for tagging, but
# every song also carries its own copy in song["_ALBUM_DATA"].
album_Data = None
USER_AGENT = "Mozilla/5.0 (X11; Linux i686; rv:135.0) Gecko/20100101 Firefox/135.0"


def get_user_data() -> tuple[Any, Any] | None:
    if not session:
        raise DeezerApiException("Error: Deezer session not initialized")

    try:
        user_data = session.get(
            "https://www.deezer.com/ajax/gw-light.php?method=deezer.getUserData&input=3&api_version=1.0&api_token="
        )
        user_data_json = user_data.json()["results"]
        options = user_data_json["USER"]["OPTIONS"]
        return options["license_token"], options["web_sound_quality"]
    except (requests.exceptions.RequestException, KeyError) as e:
        print(f"ERROR: Could not get license token: {e}")
        return None


# quality_config comes from config file
# web_sound_quality is a dict coming from Deezer API and depends on ARL cookie (premium subscription)
def set_default_song_quality(quality_config: str, web_sound_quality: dict):
    global sound_format
    flac_supported = web_sound_quality["lossless"] is True
    if flac_supported:
        if quality_config == "flac":
            sound_format = "FLAC"
        else:
            sound_format = "MP3_320"
    else:
        if quality_config == "flac":
            print(
                "WARNING: flac quality is configured in config file but not supported (no premium subscription?). Falling back to mp3"
            )
        sound_format = "MP3_128"


def get_file_format(s: dict) -> tuple[str, str]:
    if sound_format == "FLAC":
        if int(s.get("FILESIZE_FLAC", 0)) > 0:
            return ".flac", "FLAC"
        elif int(s.get("FILESIZE_MP3_320", 0)) > 0:
            print("Debug: FLAC not available, falling back to MP3_320")
            return ".mp3", "MP3_320"
        else:
            print("Debug: FLAC and MP3_320 not available, falling back to MP3_128")
            return ".mp3", "MP3_128"

    if sound_format == "MP3_320":
        if int(s.get("FILESIZE_MP3_320", 0)) > 0:
            return ".mp3", "MP3_320"
        else:
            print("Debug: MP3_320 not available, falling back to MP3_128")
            return ".mp3", "MP3_128"

    # Default
    return ".mp3", "MP3_128"


# quality is mp3 or flac
def init_deezer_session(proxy_server: str, quality: str) -> None:
    global session, license_token

    # Prefer token.env over the environment: compose injects env_file only at
    # container creation, so the environment still holds the old token after
    # /newarl rewrites the file. Imported here to keep the module importable
    # standalone.
    from utils import read_token_env_value

    deezer_token = read_token_env_value("DEEZER_TOKEN") or os.environ.get("DEEZER_TOKEN")
    if not deezer_token:
        print("Error: DEEZER_TOKEN environment variable not set")
        return

    header = {
        "Pragma": "no-cache",
        "Origin": "https://www.deezer.com",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "*/*",
        "Cache-Control": "no-cache",
        "X-Requested-With": "XMLHttpRequest",
        "Connection": "keep-alive",
        "Referer": "https://www.deezer.com/login",
        "DNT": "1",
    }
    session = requests.session()
    session.headers.update(header)
    session.cookies.update({"arl": deezer_token, "comeback": "1"})
    if len(proxy_server.strip()) > 0:
        print(f"Using proxy {proxy_server}")
        session.proxies.update({"https": proxy_server})
    user_data = get_user_data()
    if user_data is None:
        raise Exception("Error: Failed to get user data")
    license_token, web_sound_quality = user_data
    set_default_song_quality(quality, web_sound_quality)


class Deezer404Exception(Exception):
    pass


class Deezer403Exception(Exception):
    pass


class DeezerApiException(Exception):
    pass


class ScriptExtractor(html.parser.HTMLParser):
    """extract <script> tag contents from a html page"""

    def __init__(self):
        html.parser.HTMLParser.__init__(self)
        self.scripts = []
        self.curtag = None

    def handle_starttag(self, tag, attrs):
        self.curtag = tag.lower()

    def handle_data(self, data):
        if self.curtag == "script":
            self.scripts.append(data)

    def handle_endtag(self, tag):
        self.curtag = None


def md5hex(data):
    """return hex string of md5 of the given string"""
    # type(data): bytes
    # returns: bytes
    h = MD5.new()
    h.update(data)
    return b2a_hex(h.digest())


def calcbfkey(songid):
    """Calculate the Blowfish decrypt key for a given songid"""
    key = b"g4el58wc0zvf9na1"
    songid_md5 = md5hex(songid.encode())

    def xor_op(i):
        return chr(songid_md5[i] ^ songid_md5[i + 16] ^ key[i])

    decrypt_key = "".join([xor_op(i) for i in range(16)])
    return decrypt_key


def blowfishDecrypt(data, key):
    iv = a2b_hex("0001020304050607")
    c = Blowfish.new(key.encode(), Blowfish.MODE_CBC, iv)
    return c.decrypt(data)


def decryptfile(fh, key, fo):
    """
    Decrypt data from file <fh>, and write to file <fo>.
    decrypt using blowfish with <key>.
    Only every third 2048 byte block is encrypted.
    """
    blockSize = 2048
    i = 0

    for data in fh.iter_content(blockSize):
        if not data:
            break

        isEncrypted = (i % 3) == 0
        isWholeBlock = len(data) == blockSize

        if isEncrypted and isWholeBlock:
            data = blowfishDecrypt(data, key)

        fo.write(data)
        i += 1


# Upstream: get_artists() for proper multi-artist formatting
def get_artists(song: dict) -> str:
    """Build the full artist string from the per-track ARTISTS array.

    Deezer's ART_NAME only holds the lead artist, dropping co-main and
    featured artists. The ARTISTS array carries every contributor with a
    ROLE_ID ("0" = main, "5" = featured) and the correct casing, so we
    rebuild "Main1, Main2 feat. Feat1, Feat2" from it. Falls back to
    ART_NAME when the array is missing."""
    artists = song.get("ARTISTS") or []
    main, feat = [], []
    for a in artists:
        name = a.get("ART_NAME")
        if not name:
            continue
        bucket = feat if a.get("ROLE_ID") == "5" else main
        if name not in bucket:
            bucket.append(name)
    result = ", ".join(main)
    if feat:
        result = f"{result} feat. {', '.join(feat)}" if result else ", ".join(feat)
    return result or song.get("ART_NAME", "")


def downloadpicture(pic_idid):
    if not session:
        raise DeezerApiException("Error: Deezer session not initialized")

    for size in (1200, 1000):
        resp = session.get(get_picture_link(pic_idid, size))
        if resp.status_code == 200 and resp.content:
            return resp.content
    resp.raise_for_status()
    raise DeezerApiException("Error: Deezer returned empty album artwork")


def get_picture_link(pic_idid, size=1200):
    setting_domain_img = "https://e-cdns-images.dzcdn.net/images"
    return setting_domain_img + "/cover/" + pic_idid + f"/{size}x{size}.jpg"


def get_song_url(track_token: str, format: str) -> str:
    try:
        response = requests.post(
            "https://media.deezer.com/v1/get_url",
            json={
                "license_token": license_token,
                "media": [
                    {
                        "type": "FULL",
                        "formats": [{"cipher": "BF_CBC_STRIPE", "format": format}],
                    }
                ],
                "track_tokens": [
                    track_token,
                ],
            },
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            raise DeezerApiException(f"Could not retrieve song URL (401 - session expired): {e}")
        raise RuntimeError(f"Could not retrieve song URL: {e}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Could not retrieve song URL: {e}")

    if not data.get("data") or "errors" in data["data"][0]:
        raise RuntimeError(
            f"Could not get download url from API: {data['data'][0]['errors'][0]['message']}"
        )

    # Guard against empty media list (region-locked tracks)
    if not data["data"][0].get("media"):
        raise RuntimeError(
            "Could not get download url: API returned no media sources (track may not be available in your region)"
        )

    url = data["data"][0]["media"][0]["sources"][0]["url"]
    return url


def download_song(song: dict, deezer_format: str, output_file: str) -> None:
    """Downloads and decrypts the song from Deezer. Writes proper ID3/FLAC tags using mutagen."""
    assert type(song) is dict, "song must be a dict"
    assert type(output_file) is str, "output_file must be a str"

    if not session:
        raise DeezerApiException("Error: Deezer session not initialized")

    url = None
    try:
        url = get_song_url(song["TRACK_TOKEN"], deezer_format)
    except DeezerApiException:
        raise  # Session-level error (e.g. expired token), don't try fallback
    except Exception as e:
        print(
            f"Could not download song (https://www.deezer.com/us/track/{song['SNG_ID']}). Maybe it's not available anymore or at least not in your country. {e}"
        )
        if "FALLBACK" in song:
            song = song["FALLBACK"]
            print(
                f"Trying fallback song https://www.deezer.com/us/track/{song['SNG_ID']}"
            )
            try:
                url = get_song_url(song["TRACK_TOKEN"], deezer_format)
            except Exception:
                pass
            else:
                print("Fallback song seems to work")
        else:
            raise

    if url is None:
        raise Exception("Error: Failed to get song URL")

    key = calcbfkey(song["SNG_ID"])
    
    # Download and decrypt the file
    try:
        with session.get(url, stream=True) as response:
            response.raise_for_status()
            with open(output_file, "w+b") as fo:
                decryptfile(response, key, fo)
    except Exception as e:
        raise DeezerApiException(f"Could not write song to disk: {e}") from e

    # Now tag the file properly using mutagen
    _tag_audio_file(output_file, song, deezer_format)
    
    print("Download finished: {}".format(output_file))


def _tag_audio_file(output_file: str, song: dict, deezer_format: str) -> None:
    """Tag audio file with proper metadata using mutagen library."""

    # Extract metadata from song dict
    title = song.get("SNG_TITLE", "")
    # ART_NAME only holds the lead artist; get_artists() rebuilds the full
    # string from the per-track ARTISTS array (incl. co-main and featured).
    artist = get_artists(song) or song.get("ART_NAME", "")
    # Use album artist if available (for compilations)
    album_artist = song.get("ALB_ART_NAME", artist)
    album = song.get("ALB_TITLE", "")
    track_number = song.get("TRACK_NUMBER", "")
    disc_number = song.get("DISK_NUMBER", "1")
    isrc = song.get("ISRC", "")

    # Get album data if available. Prefer the per-song copy captured at fetch
    # time: the module-level `album_Data` is overwritten by every concurrent
    # metadata fetch, so it may belong to another release by the time we tag.
    year = ""
    label = ""
    album_data = song.get("_ALBUM_DATA") or album_Data
    if album_data:
        release_date = album_data.get("PHYSICAL_RELEASE_DATE", "") or album_data.get("DIGITAL_RELEASE_DATE", "")
        if release_date:
            year = release_date[:4]
        label = album_data.get("LABEL_NAME", "")
    
    # Download album art
    cover_data = None
    try:
        if song.get("ALB_PICTURE"):
            cover_data = downloadpicture(song["ALB_PICTURE"])
    except Exception as e:
        print(f"Warning: Could not download album cover: {e}")
    
    if deezer_format == "FLAC":
        _tag_flac_file(output_file, title, artist, album_artist, album, track_number, disc_number, year, label, isrc, cover_data)
    else:
        _tag_mp3_file(output_file, title, artist, album_artist, album, track_number, disc_number, year, label, isrc, cover_data)


def _tag_flac_file(output_file: str, title: str, artist: str, album_artist: str, album: str, 
                   track_number: str, disc_number: str, year: str, label: str, 
                   isrc: str, cover_data: bytes) -> None:
    """Tag FLAC file with metadata."""
    try:
        from mutagen.flac import FLAC, Picture
        
        audio = FLAC(output_file)
        
        # Clear existing tags and set new ones
        audio.delete()
        audio["title"] = title
        audio["artist"] = artist
        audio["albumartist"] = album_artist
        audio["album"] = album
        audio["tracknumber"] = str(track_number)
        audio["discnumber"] = str(disc_number)
        if year:
            audio["date"] = year
            audio["year"] = year
        if label:
            audio["label"] = label
            audio["organization"] = label
        if isrc:
            audio["isrc"] = isrc
        
        # Add album art
        if cover_data:
            image = Picture()
            image.type = 3  # Front cover
            image.mime = "image/jpeg"
            image.desc = "Cover"
            image.data = cover_data
            audio.add_picture(image)
        
        audio.save()
        print(f"Tagged FLAC file: {title} - {artist}")
    except Exception as e:
        print(f"Warning: Could not write FLAC tags: {e}")


def _tag_mp3_file(output_file: str, title: str, artist: str, album_artist: str, album: str,
                  track_number: str, disc_number: str, year: str, label: str,
                  isrc: str, cover_data: bytes) -> None:
    """Tag MP3 file with proper ID3v2.4 metadata using mutagen."""
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TDRC, TYER, TPUB, TSRC, APIC, ID3NoHeaderError
        
        # Try to load existing tags or create new ones
        try:
            audio = MP3(output_file, ID3=ID3)
        except ID3NoHeaderError:
            audio = MP3(output_file)
            audio.add_tags()
        
        if audio.tags is not None:
            audio.tags.delete(output_file)
        audio = MP3(output_file)
        if audio.tags is None:
            audio.add_tags()
        
        # Add metadata tags
        if title:
            audio.tags.add(TIT2(encoding=3, text=title))
        if artist:
            audio.tags.add(TPE1(encoding=3, text=artist))
        if album_artist:
            audio.tags.add(TPE2(encoding=3, text=album_artist))  # Album artist
        if album:
            audio.tags.add(TALB(encoding=3, text=album))
        if track_number:
            audio.tags.add(TRCK(encoding=3, text=str(track_number)))
        if disc_number:
            audio.tags.add(TPOS(encoding=3, text=str(disc_number)))
        if year:
            audio.tags.add(TDRC(encoding=3, text=year))  # Recording date (ID3v2.4)
            audio.tags.add(TYER(encoding=3, text=year))  # Year (ID3v2.3 compatibility)
        if label:
            audio.tags.add(TPUB(encoding=3, text=label))
        if isrc:
            audio.tags.add(TSRC(encoding=3, text=isrc))
        
        # Add album art
        if cover_data:
            audio.tags.add(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,  # Front cover
                    desc="Cover",
                    data=cover_data
                )
            )
        
        audio.save(v2_version=3)  # Save as ID3v2.3 for maximum compatibility
        print(f"Tagged MP3 file: {title} - {artist}")
    except Exception as e:
        print(f"Warning: Could not write MP3 tags: {e}")


def get_song_infos_from_deezer_website(search_type, id):
    # search_type: either one of the constants: TYPE_TRACK|TYPE_ALBUM|TYPE_PLAYLIST
    # id: deezer_id of the song/album/playlist (like https://www.deezer.com/de/track/823267272)
    # return: if TYPE_TRACK => song (dict grabbed from the website with information about a song)
    # return: if TYPE_ALBUM|TYPE_PLAYLIST => list of songs
    # raises
    # Deezer404Exception if
    # 1. open playlist https://www.deezer.com/de/playlist/1180748301 and click on song Honey from Moby in a new tab:
    # 2. Deezer gives you a 404: https://www.deezer.com/de/track/68925038
    # Deezer403Exception if we are not logged in

    if not session:
        raise DeezerApiException("Error: Deezer session not initialized")

    url = "https://www.deezer.com/us/{}/{}".format(search_type, id)
    resp = session.get(url)
    print(url)
    if resp.status_code == 404:
        raise Deezer404Exception("ERROR: Got a 404 for {} from Deezer".format(url))
    if "MD5_ORIGIN" not in resp.text:
        raise Deezer403Exception(
            "ERROR: we are not logged in on deezer.com. Please update the cookie"
        )

    parser = ScriptExtractor()
    parser.feed(resp.text)
    parser.close()

    songs = []
    for script in parser.scripts:
        regex = re.search(r'{"DATA":.*', script)
        if regex:
            DZR_APP_STATE = json.loads(regex.group())
            global album_Data
            album_Data = DZR_APP_STATE.get("DATA")
            if (
                DZR_APP_STATE["DATA"]["__TYPE__"] == "playlist"
                or DZR_APP_STATE["DATA"]["__TYPE__"] == "album"
            ):
                # songs if you searched for album/playlist
                for song in DZR_APP_STATE["SONGS"]["data"]:
                    song["_ALBUM_DATA"] = album_Data
                    songs.append(song)
            elif DZR_APP_STATE["DATA"]["__TYPE__"] == "song":
                # just one song on that page
                song = DZR_APP_STATE["DATA"]
                song["_ALBUM_DATA"] = album_Data
                # Fetch album artist separately — may differ from track artist on compilations
                alb_id = DZR_APP_STATE["DATA"].get("ALB_ID")
                if alb_id:
                    try:
                        alb_resp = session.get(
                            "https://www.deezer.com/us/{}/{}".format(TYPE_ALBUM, alb_id)
                        )
                        alb_parser = ScriptExtractor()
                        alb_parser.feed(alb_resp.text)
                        alb_parser.close()
                        for alb_script in alb_parser.scripts:
                            alb_regex = re.search(r'{"DATA":.*', alb_script)
                            if alb_regex:
                                alb_data = json.loads(alb_regex.group()).get("DATA", {})
                                song["ALB_ART_NAME"] = alb_data.get(
                                    "ART_NAME", song.get("ART_NAME", "")
                                )
                                break
                    except Exception:
                        pass  # Non-critical, fall back to track artist
                songs.append(song)
    return songs[0] if search_type == TYPE_TRACK else songs


def deezer_search(search, search_type):
    # search: string (What are you looking for?)
    # search_type: either one of the constants: TYPE_TRACK|TYPE_ALBUM|TYPE_ALBUM_TRACK (TYPE_PLAYLIST is not supported)
    # return: list of dicts (keys depend on search_type)

    if not session:
        raise DeezerApiException("Error: Deezer session not initialized")

    if search_type not in [TYPE_TRACK, TYPE_ALBUM, TYPE_ALBUM_TRACK]:
        print("ERROR: search_type is wrong: {}".format(search_type))
        return []
    search = urllib.parse.quote_plus(search)
    try:
        if search_type == TYPE_ALBUM_TRACK:
            data = get_song_infos_from_deezer_website(TYPE_ALBUM, search)
        else:
            resp = session.get(
                "https://api.deezer.com/search/{}?q={}".format(search_type, search)
            )
            resp.raise_for_status()
            data = resp.json()
            data = data["data"]
    except (requests.exceptions.RequestException, KeyError) as e:
        raise DeezerApiException(f"Could not search for track '{search}': {e}") from e
    return_nice = []
    for item in data:
        i = {}
        if search_type == TYPE_ALBUM:
            i["id"] = str(item["id"])
            i["id_type"] = TYPE_ALBUM
            i["album"] = item["title"]
            i["album_id"] = item["id"]
            i["img_url"] = item["cover_small"]
            i["artist"] = item["artist"]["name"]
            i["title"] = ""
            i["preview_url"] = ""

        if search_type == TYPE_TRACK:
            i["id"] = str(item["id"])
            i["id_type"] = TYPE_TRACK
            i["title"] = item["title"]
            i["img_url"] = item["album"]["cover_small"]
            i["album"] = item["album"]["title"]
            i["album_id"] = item["album"]["id"]
            i["artist"] = item["artist"]["name"]
            i["preview_url"] = item["preview"]

        if search_type == TYPE_ALBUM_TRACK:
            i["id"] = str(item["SNG_ID"])
            i["id_type"] = TYPE_TRACK
            i["title"] = item["SNG_TITLE"]
            i["img_url"] = ""  # item['album']['cover_small']
            i["album"] = item["ALB_TITLE"]
            i["album_id"] = item["ALB_ID"]
            i["artist"] = item["ART_NAME"]
            i["preview_url"] = next(
                media["HREF"] for media in item["MEDIA"] if media["TYPE"] == "preview"
            )

        return_nice.append(i)
    return return_nice


def test_deezer_login():
    print("Let's check if the deezer login is still working")
    try:
        song = get_song_infos_from_deezer_website(TYPE_TRACK, "917265")
    except (Deezer403Exception, Deezer404Exception) as msg:
        print(msg)
        print("Login is not working anymore.")
        return False

    if song:
        print("Login is still working.")
        return True
    else:
        print("Login is not working anymore.")
        return False
