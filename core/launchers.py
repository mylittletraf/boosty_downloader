import asyncio
from datetime import datetime, UTC
import json
import re
from pathlib import Path
from typing import Literal

import unicodedata

from boosty.api import get_all_image_media, get_all_video_media, get_all_posts, get_all_audio_media
from boosty.wrappers.post_pool import PostPool
from core.config import conf
from boosty.wrappers.media_pool import MediaPool
from core.logger import logger
from core.stat_tracker import stat_tracker, StatTracker
from core.utils import create_dir_if_not_exists, download_file_if_not_exists, create_text_document

def sanitize(name: str, max_length: int = 80) -> str:
    import re
    import unicodedata

    name = re.sub(r'[\\/*?:"<>|#]', '', name)
    name = "".join(c for c in name if c.isprintable())
    name = unicodedata.normalize('NFKD', name)
    name = re.sub(r'\s+', '_', name)
    name = re.sub(r'_+', '_', name)
    name = name.strip(' _.')
    name = name[:max_length]
    name = name.rstrip(' .')

    return name

async def get_file_and_raise_stat(url: str, path_file: Path, tracker: StatTracker, _t: Literal["p", "v", "a", "f"]):
    match _t:
        case "p":
            passed = tracker.add_passed_photo
            downloaded = tracker.add_downloaded_photo
            error = tracker.add_error_photo
        case "v":
            passed = tracker.add_passed_video
            downloaded = tracker.add_downloaded_video
            error = tracker.add_error_video
        case "a":
            passed = tracker.add_passed_audio
            downloaded = tracker.add_downloaded_audio
            error = tracker.add_error_audio
        case "f":
            passed = tracker.add_passed_file
            downloaded = tracker.add_downloaded_file
            error = tracker.add_error_file
        case _:
            logger.warning(f"Unknown _t: {_t}")
            return

    try:
        if await download_file_if_not_exists(url, path_file):
            downloaded()
        else:
            passed()
    except Exception as e:
        logger.warning(f"err download {url}", exc_info=e)
        error()


async def fetch_and_save_media(creator_name: str, use_cookie: bool):
    base_path: Path = conf.sync_dir / creator_name
    create_dir_if_not_exists(base_path)
    photo_path = base_path / "photos"
    video_path = base_path / "videos"
    audio_path = base_path / "audios"
    create_dir_if_not_exists(photo_path)
    create_dir_if_not_exists(video_path)
    create_dir_if_not_exists(audio_path)
    media_pool = MediaPool()
    tasks = []
    if conf.need_load_photo:
        tasks.append(
            get_all_image_media(creator_name=creator_name, media_pool=media_pool, use_cookie=use_cookie)
        )
    if conf.need_load_video:
        tasks.append(
            get_all_video_media(creator_name=creator_name, media_pool=media_pool, use_cookie=use_cookie)
        )
    if conf.need_load_audio:
        tasks.append(
            get_all_audio_media(creator_name=creator_name, media_pool=media_pool, use_cookie=use_cookie)
        )
    if conf.need_load_files:
        logger.warning("ATTACHED FILES WILL NOT BE DOWNLOADED IN MEDIA STORAGE MODE")
        logger.warning("Use storage_type: post, for download attached files")
    await asyncio.gather(*tasks)

    coros = []

    if conf.need_load_photo:
        images = media_pool.get_images()
        grp_photos = []
        i = 0
        for img in images:
            path = photo_path / (img["id"] + ".jpg")
            grp_photos.append(get_file_and_raise_stat(img["url"], path, stat_tracker, "p"))
            i += 1
            if i >= conf.max_download_parallel:
                coros.append(grp_photos)
                grp_photos = []
                i = 0
        if len(grp_photos):
            coros.append(grp_photos)

    if conf.need_load_video:
        videos = media_pool.get_videos()
        grp_videos = []
        i = 0
        for idx, video in enumerate(videos, start=1):
            title = sanitize(video.get("post_title", "video"))
            filename = f"{title}_video{idx}.mp4"
            path = video_path / filename
            grp_videos.append(get_file_and_raise_stat(video["url"], path, stat_tracker, "v"))
            i += 1
            if i == conf.max_download_parallel:
                coros.append(grp_videos)
                grp_videos = []
                i = 0
        if len(grp_videos):
            coros.append(grp_videos)

    if conf.need_load_audio:
        if use_cookie:
            audios = media_pool.get_audios()
            grp_audios = []
            i = 0
            for audio in audios:
                path = audio_path / (audio["id"] + ".mp3")
                grp_audios.append(get_file_and_raise_stat(audio["url"], path, stat_tracker, "a"))
                i += 1
                if i == conf.max_download_parallel:
                    coros.append(grp_audios)
                    grp_audios = []
                    i = 0
            if len(grp_audios):
                coros.append(grp_audios)
        else:
            logger.warning("Can't download audio without authorization. "
                           "Fill authorization fields in config to store audio files.")

    for grp in coros:
        await asyncio.gather(*grp)
        await asyncio.sleep(0)


async def fetch_and_save_posts(creator_name: str, use_cookie: bool):
    base_path: Path = conf.sync_dir / creator_name
    create_dir_if_not_exists(base_path)
    posts_path = base_path / "posts"
    create_dir_if_not_exists(posts_path)
    post_pool = PostPool()
    await get_all_posts(creator_name=creator_name, post_pool=post_pool, use_cookie=use_cookie)
    coros = []
    desired_post_id = conf.desired_post_id
    if desired_post_id:
        logger.info(f"SYNC ONLY ONE POST WITH ID = '{desired_post_id}'")
    for post in post_pool.posts:
        if desired_post_id and post.id != desired_post_id:
            continue
        clean_title = sanitize(post.title or f"post_{post.id}")
        publish_dt = datetime.fromtimestamp(post.publish_time, UTC)
        date_prefix = publish_dt.strftime("%Y-%m-%d")
        post_folder_name = f"{date_prefix}_{clean_title}"
        post_path = posts_path / post_folder_name
        if post_path.exists():
            logger.info(f"Post folder '{post_folder_name}' already exists. Skipping...")
            continue
        create_dir_if_not_exists(post_path)
        photo_path = post_path / "photos"
        video_path = post_path / "videos"
        audio_path = post_path / "audios"
        files_path = post_path / "files"
        create_dir_if_not_exists(photo_path)
        create_dir_if_not_exists(video_path)
        create_dir_if_not_exists(audio_path)
        create_dir_if_not_exists(files_path)
        await create_text_document(
            path=post_path,
            content=post.get_contents_text(),
            ext="md" if conf.post_text_in_markdown else "txt"
        )

        if conf.need_load_photo:
            images = post.media_pool.get_images()
            grp_photos = []
            i = 0
            for img in images:
                path = photo_path / (img["id"] + ".jpg")
                grp_photos.append(get_file_and_raise_stat(img["url"], path, stat_tracker, "p"))
                i += 1
                if i >= conf.max_download_parallel:
                    coros.append(grp_photos)
                    grp_photos = []
                    i = 0
            if len(grp_photos):
                coros.append(grp_photos)

        if conf.need_load_video:
            videos = post.media_pool.get_videos()
            grp_videos = []
            i = 0
            for idx, video in enumerate(videos, start=1):
                if len(videos) == 1:
                    filename = f"{clean_title}.mp4"
                else:
                    filename = f"{clean_title}_video{idx}.mp4"
                path = video_path / sanitize(filename)
                grp_videos.append(get_file_and_raise_stat(video["url"], path, stat_tracker, "v"))
                i += 1
                if i == conf.max_download_parallel:
                    coros.append(grp_videos)
                    grp_videos = []
                    i = 0
            if len(grp_videos):
                coros.append(grp_videos)

        if conf.need_load_audio:
            if use_cookie:
                audios = post.media_pool.get_audios()
                grp_audios = []
                i = 0
                for audio in audios:
                    path = audio_path / (audio["id"] + ".mp3")
                    grp_audios.append(get_file_and_raise_stat(audio["url"], path, stat_tracker, "a"))
                    i += 1
                    if i == conf.max_download_parallel:
                        coros.append(grp_audios)
                        grp_audios = []
                        i = 0
                if len(grp_audios):
                    coros.append(grp_audios)
            else:
                logger.warning("Can't download audio without authorization. "
                               "Fill authorization fields in config to store audio files.")

        if conf.need_load_files:
            if use_cookie:
                files = post.media_pool.get_files()
                grp_files = []
                i = 0
                for file in files:
                    path = files_path / file["title"]
                    grp_files.append(get_file_and_raise_stat(file["url"], path, stat_tracker, "f"))
                    i += 1
                    if i == conf.max_download_parallel:
                        coros.append(grp_files)
                        grp_files = []
                        i = 0
                if len(grp_files):
                    coros.append(grp_files)
            else:
                logger.warning("Can't download attached files without authorization. "
                               "Fill authorization fields in config to store attached files.")

    for grp in coros:
        await asyncio.gather(*grp)
        await asyncio.sleep(0)

