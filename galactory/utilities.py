# -*- coding: utf-8 -*-
# (c) 2022 Brian Scholer (@briantist)

import json
import semver
import math
import hashlib

from tempfile import SpooledTemporaryFile
from urllib.request import urlopen
from urllib3 import Retry
from requests.adapters import HTTPAdapter
from requests import Session

from flask import url_for, request, current_app
from artifactory import ArtifactoryPath
from dohq_artifactory.auth import XJFrogArtApiAuth


def _session_with_retries(retry=None, auth=None) -> Session:
    if retry is None:
        retry = Retry(connect=5, read=3, redirect=2, status=6, other=3, backoff_factor=0.1, raise_on_status=False)

    adapter = HTTPAdapter(max_retries=retry)
    session = Session()
    session.auth = auth
    session.mount('http://', adapter)
    session.mount('https://', adapter)

    return session


def authorize(request, artifactory_path, retry=None) -> ArtifactoryPath:
    auth = None
    apikey = current_app.config['ARTIFACTORY_API_KEY']

    if current_app.config['USE_GALAXY_KEY'] and (not current_app.config['PREFER_CONFIGURED_KEY'] or not apikey):
        authorization = request.headers.get('Authorization')
        if authorization:
            apikey = authorization.split(' ')[1]

    if apikey:
        auth = XJFrogArtApiAuth(apikey)

    session = _session_with_retries(retry=retry, auth=auth)
    return ArtifactoryPath(artifactory_path, session=session)


# TODO: this relies on a paid feature
# We can work around it by parsing the archives as we upload,
# and extracting the manifest at that time. We're already now
# adding the important part (collection_info) as its own
# property, so all read operations will be able to get it
# that way in the future.
def load_manifest_from_artifactory(artifact):
    with urlopen(str(artifact) + '!/MANIFEST.json') as u:
        manifest = json.load(u)
    return manifest


def discover_collections(repo, namespace=None, name=None, version=None, fast_detection=True):
    for p in repo:
        if fast_detection:
            # we're going to use the naming convention to eliminate candidates early,
            # to avoid excessive additional requests for properties and stat that slow
            # down the listing immensely as the number of collections grows.
            try:
                f_namespace, f_name, f_version = p.name.replace('.tar.gz', '').split('-')
            except ValueError:
                pass
            else:
                if not all(
                    (
                        not namespace or f_namespace == namespace,
                        not name or f_name == name,
                        not version or f_version == version
                    )
                ):
                    continue

        info = p.stat()
        if info.is_dir:
            continue

        props = p.properties
        if not props.get('version'): # TODO: change to collection_info
            continue

        if 'collection_info' in props:
            collection_info = json.loads(props['collection_info'][0])
        else:
            # fallback for now just in case, we expect this never to be hit
            # TODO: remove in the next version
            collection_info = load_manifest_from_artifactory(p)['collection_info']

        coldata = {
            'collection_info': collection_info,
            'fqcn': props['fqcn'][0],
            'created': info.ctime.isoformat(),
            'modified': info.mtime.isoformat(),
            'namespace': {'name': props['namespace'][0]},
            'name': props['name'][0],
            'filename': p.name,
            'sha256': info.sha256,
            'size': info.size,
            'download_url': url_for(
                'download.download',
                filename=p.name,
                _external=True,
            ),
            # 'download_url': str(p),
            'mime_type': info.mime_type,
            'version': props['version'][0],
            'semver': semver.VersionInfo.parse(props['version'][0]),
        }

        if all(
            (
                not namespace or coldata['namespace']['name'] == namespace,
                not name or coldata['name'] == name,
                not version or coldata['version'] == version
            )
        ):
            yield coldata


def collected_collections(repo, namespace=None, name=None):
    collections = {}

    for c in discover_collections(repo, namespace=namespace, name=name):
        version = c['version']
        ver = c['semver']
        col = collections.setdefault(c['fqcn'], {})
        versions = col.setdefault('versions', {})
        versions[version] = c
        if not ver.prerelease:
            try:
                latest = col['latest']
            except KeyError:
                col['latest'] = c
            else:
                if ver > latest['semver']:
                    col['latest'] = c

    return collections


def _collection_listing(repo, namespace=None, collection=None):
    collections = collected_collections(repo, namespace, collection)

    results = []

    for _, i in collections.items():
        latest = i['latest']

        result = {
            'href': request.url,
            'name': latest['name'],
            'namespace': latest['namespace'],
            'created': latest['created'],
            'modified': latest['modified'],
            'versions_url': url_for(
                'api.v2.versions',
                namespace=latest['namespace']['name'],
                collection=latest['name'],
                _external=True,
            ),
            'latest_version': {
                'href': url_for(
                    'api.v2.version',
                    namespace=latest['namespace']['name'],
                    collection=latest['name'],
                    version=latest['version'],
                    _external=True,
                ),
                "version": latest['version'],
            }
        }
        results.append(result)

    return results


def lcm(a, b, *more):
    z = lcm(b, *more) if more else b
    return abs(a * z) // math.gcd(a, z)


class HashedTempFile():
    def __init__(self, handle, md5, sha1, sha256, close=True) -> None:
        self.handle = handle
        self.md5 = md5
        self.sha1 = sha1
        self.sha256 = sha256
        self._close = close

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        if self._close:
            self.handle.close()


def _chunk_to_temp(fsrc, iterator=None, spool_size=5*1024*1024, seek_to_zero=True, chunk_multiplier=64, close=True) -> HashedTempFile:
    md5sum = hashlib.md5()
    sha1sum = hashlib.sha1()
    sha256sum = hashlib.sha256()
    common_block_size = lcm(md5sum.block_size, sha1sum.block_size, sha256sum.block_size)
    chunk_size = chunk_multiplier * common_block_size

    it = iter(lambda: fsrc.read(chunk_size), b'') if iterator is None else iterator(chunk_size)

    tmp = SpooledTemporaryFile(max_size=spool_size)

    for chunk in it:
        md5sum.update(chunk)
        sha1sum.update(chunk)
        sha256sum.update(chunk)
        tmp.write(chunk)

    if seek_to_zero:
        tmp.seek(0)

    return HashedTempFile(tmp, md5sum.hexdigest(), sha1sum.hexdigest(),  sha256sum.hexdigest(), close=close)
