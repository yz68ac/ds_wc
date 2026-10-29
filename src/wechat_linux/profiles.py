"""Declared compatibility, not proof of an authenticated online client."""
from importlib.resources import files
import json
import sys


def capabilities():
    profile=json.loads(files('wechat_linux.native').joinpath('profile.json').read_text(encoding='utf-8'))
    image=json.loads(files('wechat_linux.native').joinpath('image_profile.json').read_text(encoding='utf-8'))
    return dict(sdk_version='0.2.0',platform=sys.platform,platform_supported=sys.platform=='linux',
                features=['read_messages','parse_mentions','parse_quotes','parse_pat','read_images',
                          'desktop_image_download','native_text_submission','native_image_submission','native_image_download'],
                native_profiles=[{key:p[key] for key in ('version','arch','sha256','backend')} for p in (profile,image)],
                native_image_download=dict(enabled_by_default=False,requires='explicitly enabled isolated helper',
                                           quality='cached'),
                native_image_submission=dict(formats=['image/png','image/jpeg'],input='local_path',
                                             max_bytes=8*1024*1024,runtime_verified=False,
                                             requires='version-verified helper; retain the source file until delivery confirmation'),
                desktop_image_download_requires=['X11','AT-SPI','GdkPixbuf','window manager'],
                native_text_submission_requires='separately installed, version-verified privileged helper')
