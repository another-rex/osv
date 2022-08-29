# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Ecosystem helpers."""

import bisect
import json
import logging

import packaging.version
import urllib.parse
import requests

from .third_party.univers.debian import Version as DebianVersion
from .third_party.univers.gem import GemVersion

from . import debian_version_cache
from . import maven
from . import nuget
from . import semver_index
from .cache import Cache
from .cache import cached
from .request_helper import RequestError, RequestHelper

_DEPS_DEV_API = (
    'https://api.deps.dev/insights/v1alpha/systems/{ecosystem}/packages/'
    '{package}/versions')
use_deps_dev = False
deps_dev_api_key = ''

shared_cache: Cache = None


class EnumerateError(Exception):
  """Non-retryable version enumeration error."""


class DepsDevMixin:
  """deps.dev mixin."""

  _DEPS_DEV_ECOSYSTEM_MAP = {
      'Maven': 'MAVEN',
      'PyPI': 'PYPI',
  }

  def _deps_dev_enumerate(self, package, introduced, fixed, limits=None):
    """Use deps.dev to get list of versions."""
    ecosystem = self._DEPS_DEV_ECOSYSTEM_MAP[self.name]
    url = _DEPS_DEV_API.format(ecosystem=ecosystem, package=package)
    response = requests.get(
        url, headers={
            'X-DepsDev-APIKey': deps_dev_api_key,
        })

    if response.status_code == 404:
      raise EnumerateError(f'Package {package} not found')
    if response.status_code != 200:
      raise RuntimeError(
          f'Failed to get {ecosystem} versions for {package} with: '
          f'{response.text}')

    response = response.json()
    versions = [v['version'] for v in response['versions']]
    self.sort_versions(versions)
    return self._get_affected_versions(versions, introduced, fixed, limits)


class Ecosystem:
  """Ecosystem helpers."""

  @property
  def name(self):
    """Get the name of the ecosystem."""
    return self.__class__.__name__

  def _before_limits(self, version, limits):
    """Return whether or not the given version is before any limits."""
    if not limits or '*' in limits:
      return True

    return any(
        self.sort_key(version) < self.sort_key(limit) for limit in limits)

  def next_version(self, package, version):
    """Get the next version after the given version."""
    versions = self.enumerate_versions(package, version, fixed=None)
    if versions and versions[0] != version:
      # Version does not exist, so use the first one that would sort
      # after it (which is what enumerate_versions returns).
      return versions[0]

    if len(versions) > 1:
      return versions[1]

    return None

  def sort_key(self, version):
    """Sort key."""
    raise NotImplementedError

  def sort_versions(self, versions):
    """Sort versions."""
    versions.sort(key=self.sort_key)

  def enumerate_versions(self, package, introduced, fixed, limits=None):
    """Enumerate versions."""
    raise NotImplementedError

  def _get_affected_versions(self, versions, introduced, fixed, limits):
    """Get affected versions.

    Args:
      versions: a list of version strings.
      introduced: a list of version strings.
      fixed: a list of version strings.
      limits: a list of version strings.

    Returns:
      A list of affected version strings.
    """
    parsed_versions = [self.sort_key(v) for v in versions]

    if introduced == '0':
      introduced = None

    if introduced:
      introduced = self.sort_key(introduced)
      start_idx = bisect.bisect_left(parsed_versions, introduced)
    else:
      start_idx = 0

    if fixed:
      fixed = self.sort_key(fixed)
      end_idx = bisect.bisect_left(parsed_versions, fixed)
    else:
      end_idx = len(versions)

    affected = versions[start_idx:end_idx]
    return [v for v in affected if self._before_limits(v, limits)]

  @property
  def is_semver(self):
    return False


class SemverEcosystem(Ecosystem):
  """Generic semver ecosystem helpers."""

  def sort_key(self, version):
    """Sort key."""
    return semver_index.parse(version)

  def enumerate_versions(self, package, introduced, fixed, limits=None):
    """Enumerate versions (no-op)."""
    del package
    del introduced
    del fixed
    del limits

  def next_version(self, package, version):
    """Get the next version after the given version."""
    del package  # Unused.
    parsed_version = semver_index.parse(version)
    if parsed_version.prerelease:
      return version + '.0'

    return str(parsed_version.bump_patch()) + '-0'

  @property
  def is_semver(self):
    return True


Crates = SemverEcosystem
Go = SemverEcosystem
NPM = SemverEcosystem


class PyPI(Ecosystem):
  """PyPI ecosystem helpers."""

  _API_PACKAGE_URL = 'https://pypi.org/pypi/{package}/json'

  def sort_key(self, version):
    """Sort key."""
    return packaging.version.parse(version)

  def enumerate_versions(self, package, introduced, fixed, limits=None):
    """Enumerate versions."""
    response = requests.get(self._API_PACKAGE_URL.format(package=package))

    if response.status_code == 404:
      raise EnumerateError(f'Package {package} not found')
    if response.status_code != 200:
      raise RuntimeError(
          f'Failed to get PyPI versions for {package} with: {response.text}')

    response = response.json()
    versions = list(response['releases'].keys())
    self.sort_versions(versions)

    return self._get_affected_versions(versions, introduced, fixed, limits)


class Maven(Ecosystem, DepsDevMixin):
  """Maven ecosystem."""

  _API_PACKAGE_URL = 'https://search.maven.org/solrsearch/select'

  def sort_key(self, version):
    """Sort key."""
    return maven.Version.from_string(version)

  @staticmethod
  def _get_versions(package):
    """Get versions."""
    versions = []
    request_helper = RequestHelper()

    group_id, artifact_id = package.split(':', 2)
    start = 0

    while True:
      query = {
          'q': f'g:"{group_id}" AND a:"{artifact_id}"',
          'core': 'gav',
          'rows': '20',
          'wt': 'json',
          'start': start
      }
      url = Maven._API_PACKAGE_URL + '?' + urllib.parse.urlencode(query)
      response = request_helper.get(url)
      response = json.loads(response)['response']
      if response['numFound'] == 0:
        raise EnumerateError(f'Package {package} not found')

      for result in response['docs']:
        versions.append(result['v'])

      if len(versions) >= response['numFound']:
        break

      start = len(versions)

    return versions

  def enumerate_versions(self, package, introduced, fixed, limits=None):
    """Enumerate versions."""
    if use_deps_dev:
      return self._deps_dev_enumerate(package, introduced, fixed, limits=limits)

    get_versions = self._get_versions
    if shared_cache:
      get_versions = cached(shared_cache)(get_versions)

    versions = get_versions(package)
    self.sort_versions(versions)
    return self._get_affected_versions(versions, introduced, fixed, limits)


class RubyGems(Ecosystem):
  """RubyGems ecosystem."""

  _API_PACKAGE_URL = 'https://rubygems.org/api/v1/versions/{package}.json'

  def sort_key(self, version):
    """Sort key."""
    return GemVersion(version)

  def enumerate_versions(self, package, introduced, fixed, limits=None):
    """Enumerate versions."""
    response = requests.get(self._API_PACKAGE_URL.format(package=package))
    if response.status_code == 404:
      raise EnumerateError(f'Package {package} not found')
    if response.status_code != 200:
      raise RuntimeError(
          f'Failed to get RubyGems versions for {package} with: {response.text}'
      )

    response = response.json()
    versions = [entry['number'] for entry in response]

    self.sort_versions(versions)
    return self._get_affected_versions(versions, introduced, fixed, limits)


class NuGet(Ecosystem):
  """NuGet ecosystem."""

  _API_PACKAGE_URL = ('https://api.nuget.org/v3/registration5-semver1/'
                      '{package}/index.json')

  def sort_key(self, version):
    """Sort key."""
    return nuget.Version.from_string(version)

  def enumerate_versions(self, package, introduced, fixed, limits=None):
    """Enumerate versions."""
    url = self._API_PACKAGE_URL.format(package=package.lower())
    response = requests.get(url)
    if response.status_code == 404:
      raise EnumerateError(f'Package {package} not found')
    if response.status_code != 200:
      raise RuntimeError(
          f'Failed to get NuGet versions for {package} with: {response.text}')

    response = response.json()

    versions = []
    for page in response['items']:
      if 'items' in page:
        items = page['items']
      else:
        items_response = requests.get(page['@id'])
        if items_response.status_code != 200:
          raise RuntimeError(
              f'Failed to get NuGet versions page for {package} with: '
              f'{response.text}')

        items = items_response.json()['items']

      for item in items:
        versions.append(item['catalogEntry']['version'])

    self.sort_versions(versions)
    return self._get_affected_versions(versions, introduced, fixed, limits)


class Debian(Ecosystem):
  """Debian ecosystem"""

  _API_PACKAGE_URL = 'https://snapshot.debian.org/mr/package/{package}/'
  debian_release_ver: str

  def __init__(self, debian_release_ver: str):
    self.debian_release_ver = debian_release_ver

  def sort_key(self, version):
    if not DebianVersion.is_valid(version):
      # If debian version is not valid, it is most likely an invalid fixed
      # version then sort it to the last/largest element
      return DebianVersion(999999, 999999)
    return DebianVersion.from_string(version)

  def enumerate_versions(self, package, introduced, fixed, limits=None):
    url = self._API_PACKAGE_URL.format(package=package.lower())
    request_helper = RequestHelper(shared_cache)
    try:
      text_response = request_helper.get(url)
    except RequestError as ex:
      if ex.response.status_code == 404:
        raise EnumerateError(f'Package {package} not found') from ex
      raise RuntimeError('Failed to get Debian versions for '
                         f'{package} with: {ex.response.text}') from ex

    response = json.loads(text_response)
    raw_versions: list[str] = [x['version'] for x in response['result']]

    # Remove rare cases of unknown versions
    def version_is_valid(v):
      if not DebianVersion.is_valid(v):
        logging.warning('Package %s has invalid version: %s', package, v)
        return False

      return True

    versions = [v for v in raw_versions if version_is_valid(v)]
    # Sort to ensure it is in the correct order
    versions.sort(key=self.sort_key)
    # The only versions with +deb
    versions = [
        x for x in versions
        if '+deb' not in x or f'+deb{self.debian_release_ver}' in x
    ]

    if introduced == '0':
      # Update introduced to the first version of the debian version
      introduced = debian_version_cache.get_first_package_version(
          package, self.debian_release_ver)

    if not DebianVersion.is_valid(fixed):
      logging.warning(
          'Package %s has invalid fixed version: %s. In debian release %s',
          package, fixed, self.debian_release_ver)
      return []

    return self._get_affected_versions(versions, introduced, fixed, limits)


_ecosystems = {
    'crates.io': Crates(),
    'Go': Go(),
    'Maven': Maven(),
    'npm': NPM(),
    'NuGet': NuGet(),
    'PyPI': PyPI(),
    'RubyGems': RubyGems(),
}

SEMVER_ECOSYSTEMS = {
    'crates.io',
    'Go',
    'npm',
}


def get(name: str) -> Ecosystem:
  """Get ecosystem helpers for a given ecosystem."""

  if name.startswith('Debian:'):
    return Debian(name.split(':')[1])

  return _ecosystems.get(name)


def set_cache(cache: Cache):
  """Configures and enables the redis caching layer"""
  global shared_cache
  shared_cache = cache


def normalize(ecosystem_name: str):
  return ecosystem_name.split(':')[0]
