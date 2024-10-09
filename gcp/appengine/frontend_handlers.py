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
"""Handlers for the OSV web frontend."""

import json
import os
import math
import re
import logging

from flask import abort
from flask import current_app
from flask import Blueprint
from flask import make_response
from flask import redirect
from flask import render_template, render_template_string
from flask import request
from flask import url_for
from flask import send_from_directory
from werkzeug.security import safe_join
from werkzeug import exceptions
from google.cloud import ndb
from cvss import CVSS2, CVSS3, CVSS4

import markdown2
from urllib import parse

import cache
import osv
import rate_limiter
import source_mapper
import utils

blueprint = Blueprint('frontend_handlers', __name__)

_PAGE_SIZE = 16
_PAGE_LOOKAHEAD = 4
_REQUESTS_PER_MIN = 30
_WORD_CHARACTERS_OR_DASH = re.compile(r'^[+\w-]+$')
_WORD_CHARACTERS_OR_DASH_OR_COLON = re.compile(r'^[+\w:-]+$')
_VALID_BLOG_NAME = _WORD_CHARACTERS_OR_DASH
_VALID_VULN_ID = _WORD_CHARACTERS_OR_DASH_OR_COLON
_BLOG_CONTENTS_DIR = 'blog'
_DEPS_BASE_URL = 'https://deps.dev'
_FIRST_CVSS_CALCULATOR_BASE_URL = 'https://www.first.org/cvss/calculator'

if utils.is_prod():
  redis_host = os.environ.get('REDISHOST', 'localhost')
  redis_port = int(os.environ.get('REDISPORT', 6379))
  limiter = rate_limiter.RateLimiter(
      redis_host, redis_port, requests_per_min=_REQUESTS_PER_MIN)

  @blueprint.before_request
  def check_rate_limit():
    # TODO(michaelkedar): Cloud Run/App Engine have different ways to check this
    # remove the App Engine header check when moving away from App Engine
    ip_addr = request.headers.get('X-Appengine-User-Ip')
    if ip_addr is None:
      ip_addr = request.headers.get('X-Forwarded-For', 'unknown').split(',')[0]
    if not limiter.check_request(ip_addr):
      abort(429)


def _load_blog_content(name):
  """Load blog content."""
  path = os.path.join(current_app.static_folder, _BLOG_CONTENTS_DIR, name)
  if not os.path.exists(path):
    abort(404)
    return None

  with open(path) as handle:
    return handle.read()


@blueprint.before_request
def check_cors_preflight():
  """Handle CORS preflight requests."""
  if request.method != 'OPTIONS':
    return None

  response = make_response()
  response.headers.add('Access-Control-Allow-Origin', 'http://localhost:8080')
  response.headers.add('Access-Control-Allow-Methods', '*')
  response.headers.add('Access-Control-Allow-Headers', '*')
  return response


@blueprint.after_request
def add_cors_headers(response):
  """Add CORS headers."""
  response.headers.add('Access-Control-Allow-Origin', 'http://localhost:8080')
  return response


@blueprint.route('/v2/')
def index_v2():
  return redirect('/')


@blueprint.route('/v2/<path:subpath>')
def index_v2_with_subpath(subpath):
  return redirect('/' + subpath)


@blueprint.route('/')
def index():
  return render_template(
      'home.html', ecosystem_counts=osv_get_ecosystem_counts_cached())


@blueprint.route('/robots.txt')
def robots():
  response = make_response(f'Sitemap: {request.host_url}sitemap_index.xml\n')
  response.mimetype = 'text/plain'
  return response


@blueprint.route('/blog/', strict_slashes=False)
def blog():
  return render_template('blog.html', index=_load_blog_content('index.html'))


@blueprint.route('/blog/index.xml')
def blog_rss():
  return current_app.send_static_file(
      os.path.join(_BLOG_CONTENTS_DIR, 'index.xml'))


@blueprint.route('/blog/posts/<blog_name>/', strict_slashes=False)
def blog_post(blog_name):
  if not _VALID_BLOG_NAME.match(blog_name):
    abort(404)

  path = safe_join('posts', blog_name, 'index.html')
  if not path:
    abort(404)

  return render_template(
      'blog_post.html',
      content=_load_blog_content(safe_join('posts', blog_name, 'index.html')))


@blueprint.route('/blog/posts/<blog_name>/<file_name>', strict_slashes=False)
def blog_post_static_files(blog_name: str, file_name: str):
  """Return static files under blog post directories"""
  if not _VALID_BLOG_NAME.match(blog_name):
    abort(404)

  path = safe_join(current_app.static_folder, _BLOG_CONTENTS_DIR, 'posts',
                   blog_name)
  if not path:
    abort(404)

  return send_from_directory(path, file_name)


@blueprint.route('/about')
def about():
  return redirect('https://google.github.io/osv.dev/faq')


@blueprint.route('/faq')
def faq():
  return redirect('https://google.github.io/osv.dev/faq')


@blueprint.route('/docs', strict_slashes=False)
def docs():
  return redirect('https://google.github.io/osv.dev')


@blueprint.route('/ecosystems')
def ecosystems():
  return redirect('https://osv-vulnerabilities.storage.googleapis.com/ecosystems.txt')  # pylint: disable=line-too-long


_LIST_ARGS = ['q', 'ecosystem', 'page']


@blueprint.route('/list')
def list_vulnerabilities():
  """Main page."""

  # Remove unknown query parameters
  args = {k: v for k, v in request.args.lists() if k in _LIST_ARGS}

  # Remove page parameter if not from turbo frame
  is_turbo_frame = request.headers.get('Turbo-Frame')
  if not is_turbo_frame and args.get('page', 1) != 1:
    args.pop('page', None)

  # redirect if any query parameters were filtered
  if args.keys() != request.args.keys():
    return redirect(url_for(request.endpoint, **args))

  query = request.args.get('q', '')
  # Remove leading and trailing spaces
  query = query.strip()
  page = int(request.args.get('page', 1))
  ecosystem = request.args.get('ecosystem')

  if page < 0:
    args.pop('page', None)
    return redirect(url_for(request.endpoint, **args))

  results = osv_query(query, page, False, ecosystem)

  # Fetch ecosystems by default. As an optimization, skip when rendering page
  # fragments.
  ecosystem_counts = osv_get_ecosystem_counts_cached(
  ) if not is_turbo_frame else None

  return render_template(
      'list.html',
      page=page,
      total_pages=math.ceil(results['total'] / _PAGE_SIZE),
      query=query,
      selected_ecosystem=ecosystem,
      ecosystem_counts=ecosystem_counts,
      vulnerabilities=results['items'])


@blueprint.route('/vulnerability/<vuln_id>')
def vulnerability(vuln_id):
  """Vulnerability page."""
  vuln = osv_get_by_id(vuln_id)

  if utils.is_prod():
    api_url = 'api.osv.dev'
  else:
    api_url = 'api.test.osv.dev'

  return render_template(
      'vulnerability.html', vulnerability=vuln, api_url=api_url)


@blueprint.route('/<potential_vuln_id>')
def vulnerability_redirector(potential_vuln_id):
  """Convenience redirector for /VULN-ID to /vulnerability/VULN-ID."""
  # AlmaLinux have colons in their identifiers, which gets URL encoded.
  potential_vuln_id = parse.unquote(potential_vuln_id)
  if not _VALID_VULN_ID.match(potential_vuln_id):
    abort(404)
    return None

  vuln = osv_get_by_id(potential_vuln_id)
  if vuln:
    return redirect(f'/vulnerability/{potential_vuln_id}')

  abort(404)
  return None


@blueprint.route('/<potential_vuln_id>.json')
@blueprint.route('/vulnerability/<potential_vuln_id>.json')
def vulnerability_json_redirector(potential_vuln_id):
  """Convenience redirector for /VULN-ID.json and /vulnerability/VULN-ID.json to
  https://api.osv.dev/v1/vulns/VULN-ID.
  """
  if not _VALID_VULN_ID.match(potential_vuln_id):
    abort(404)
    return None

  vuln = osv_get_by_id(potential_vuln_id)
  if not vuln:
    abort(404)
    return None

  if utils.is_prod():
    api_url = 'api.osv.dev'
  else:
    api_url = 'api.test.osv.dev'
  return redirect(f'https://{api_url}/v1/vulns/{potential_vuln_id}')


def bug_to_response(bug, detailed=True):
  """Convert a Bug entity to a response object."""
  response = osv.vulnerability_to_dict(
      bug.to_vulnerability(include_alias=detailed))
  response.update({
      'isFixed': bug.is_fixed,
      'invalid': bug.status == osv.BugStatus.INVALID
  })

  add_cvss_score(response)

  if detailed:
    add_links(response)
    add_source_info(bug, response)
  return response


def calculate_severity_details(
    severity: dict) -> tuple[float | None, str | None]:
  """Calculate score and rating of severity"""
  cvss_calculator = {
      'CVSS_V2': CVSS2,
      'CVSS_V3': CVSS3,
      'CVSS_V4': CVSS4,
  }

  type_ = severity.get('type')
  score = severity.get('score')

  if not (type_ and score):
    return None, None

  try:
    c = cvss_calculator[type_](score)
    severity_rating = c.severities()[0]
  except Exception as e:
    logging.error('Exception raised when parsing "%s" severity "%s": %s', type_,
                  score, e)
    return None, None

  severity_score = c.base_score
  return severity_score, severity_rating


def add_cvss_score(bug):
  """Add severity score where possible."""
  severity_score = None
  severity_rating = None
  severity_type = None

  for severity in bug.get('severity', []):
    type_ = severity.get('type')
    if type_ and (not severity_type or type_ > severity_type):
      severity_type = type_
      severity_score, severity_rating = calculate_severity_details(severity)

  bug['severity_score'] = severity_score
  bug['severity_rating'] = severity_rating


def add_links(bug):
  """Add VCS links where possible."""

  first_repo_url = None

  for entry in bug.get('affected', []):
    for i, affected_range in enumerate(entry.get('ranges', [])):
      affected_range['id'] = i
      if affected_range['type'] != 'GIT':
        continue

      repo_url = affected_range.get('repo')
      if not repo_url:
        continue

      if not first_repo_url:
        first_repo_url = repo_url

      for event in affected_range.get('events', []):
        if event.get('introduced') and event['introduced'] != '0':
          event['introduced_link'] = _commit_to_link(repo_url,
                                                     event['introduced'])
          continue

        if event.get('last_affected'):
          event['last_affected_link'] = _commit_to_link(repo_url,
                                                        event['last_affected'])
          continue

        if event.get('fixed'):
          event['fixed_link'] = _commit_to_link(repo_url, event['fixed'])
          continue

        if event.get('limit'):
          event['limit_link'] = _commit_to_link(repo_url, event['limit'])
          continue

  if first_repo_url:
    bug['repo'] = first_repo_url


def add_source_info(bug, response):
  """Add upstream provenance information to `response`."""
  if bug.source_of_truth == osv.SourceOfTruth.INTERNAL:
    response['source'] = 'INTERNAL'
    return

  source_repo = osv.get_source_repository(bug.source)
  if not source_repo or not source_repo.link:
    logging.error(
        'Unexpected state for "%s": source repository/link not found for "%s"',
        bug.id, bug.source)
    return

  source_path = osv.source_path(source_repo, bug)
  response['source'] = source_repo.link + source_path
  response['source_link'] = response['source']
  if source_repo.human_link:
    bug_ecosystems = bug.ecosystem
    bug_id = bug.id()
    response['human_source_link'] = render_template_string(
        source_repo.human_link, ECOSYSTEMS=bug_ecosystems, BUG_ID=bug_id)


def _commit_to_link(repo_url, commit):
  """Convert commit to link."""
  vcs = source_mapper.get_vcs_viewer_for_url(repo_url)
  if not vcs:
    return None

  if ':' not in commit:
    return vcs.get_source_url_for_revision(commit)

  commit_parts = commit.split(':')
  if len(commit_parts) != 2:
    return None

  start, end = commit_parts
  if start == 'unknown':
    return None

  return vcs.get_source_url_for_revision_diff(start, end)


def osv_get_ecosystems():
  """Get list of ecosystems."""
  query = osv.Bug.query(projection=[osv.Bug.ecosystem], distinct=True)
  return sorted([bug.ecosystem[0] for bug in query if bug.ecosystem],
                key=str.lower)


@cache.smart_cache(
    "osv_get_ecosystem_counts", hard_timeout=24 * 60 * 60, soft_timeout=30 * 60)
def osv_get_ecosystem_counts_cached():
  """Get count of vulnerabilities per ecosystem, cached"""
  # Check if we're already in ndb context, if not, put us in one
  # We can sometimes not be in ndb context because caching
  # runs in a separate thread
  if ndb.get_context(raise_context_error=False) is None:
    # IMPORTANT: Ensure this ndb.Client remains consistent
    # with the one defined in main.py
    with ndb.Client().context():
      return osv_get_ecosystem_counts()

  return osv_get_ecosystem_counts()


def osv_get_ecosystem_counts() -> dict[str, int]:
  """Get count of vulnerabilities per ecosystem."""
  counts = {}
  ecosystem_names = osv_get_ecosystems()
  for ecosystem in ecosystem_names:
    if ':' in ecosystem:
      # Count by the base ecosystem index. Otherwise we'll overcount as a
      # single entry may refer to multiple sub-ecosystems.
      continue

    counts[ecosystem] = osv.Bug.query(
        osv.Bug.ecosystem == ecosystem,
        osv.Bug.public == True,  # pylint: disable=singleton-comparison
        osv.Bug.status == osv.BugStatus.PROCESSED).count()

  filtered_counts = {key: elem for key, elem in counts.items() if elem > 0}
  return filtered_counts


def osv_query(search_string, page, affected_only, ecosystem):
  """Run an OSV query."""
  query: ndb.Query = osv.Bug.query(osv.Bug.status == osv.BugStatus.PROCESSED,
                                   osv.Bug.public == True)  # pylint: disable=singleton-comparison

  if search_string:
    query = query.filter(osv.Bug.search_indices == search_string.lower())

  if affected_only:
    query = query.filter(osv.Bug.has_affected == True)  # pylint: disable=singleton-comparison

  if ecosystem:
    query = query.filter(osv.Bug.ecosystem == ecosystem)

  query = query.order(-osv.Bug.timestamp)

  if not search_string and not affected_only:
    # If no search string and not affected only, use the cached ecosystem counts
    total_future = ndb.Future()
    total_future.set_result(get_vuln_count_for_ecosystem(ecosystem))
  else:
    total_future = query.count_async()

  result_items = []

  bugs, _, _ = query.fetch_page(
      page_size=_PAGE_SIZE, offset=(page - 1) * _PAGE_SIZE)
  for bug in bugs:
    result_items.append(bug_to_response(bug, detailed=False))

  results = {
      'total': total_future.get_result(),
      'items': result_items,
  }

  return results


def get_vuln_count_for_ecosystem(ecosystem: str) -> int:
  ecosystem_counts = osv_get_ecosystem_counts_cached()
  if not ecosystem:
    return sum(ecosystem_counts.values())

  return ecosystem_counts.get(ecosystem, 0)


def osv_get_by_id(vuln_id):
  """Gets bug details from its id. If invalid, aborts the request."""
  if not vuln_id:
    abort(400)
    return None

  bug = osv.Bug.get_by_id(vuln_id)
  if not bug:
    abort(404)
    return None

  if bug.status == osv.BugStatus.UNPROCESSED:
    abort(404)
    return None

  if not bug.public:
    abort(403)
    return None

  return bug_to_response(bug)


@blueprint.app_template_filter('event_type')
def event_type(event):
  """Get the type from an event."""
  if event.get('introduced'):
    return 'Introduced'
  if event.get('fixed'):
    return 'Fixed'
  if event.get('limit'):
    return 'Limit'
  if event.get('last_affected'):
    return 'Last affected'

  return None


@blueprint.app_template_filter('event_link')
def event_link(event):
  """Get the link from an event."""
  if event.get('introduced_link'):
    return event['introduced_link']
  if event.get('fixed_link'):
    return event['fixed_link']
  if event.get('limit_link'):
    return event['limit_link']
  if event.get('last_affected_link'):
    return event['last_affected_link']

  return None


@blueprint.app_template_filter('event_value')
def event_value(event):
  """Get the value from an event."""
  if event.get('introduced'):
    return event['introduced']
  if event.get('fixed'):
    return event['fixed']
  if event.get('limit'):
    return event['limit']
  if event.get('last_affected'):
    return event['last_affected']

  return None


@blueprint.app_template_filter('should_collapse')
def should_collapse(affected):
  """Whether if we should collapse the package tab bar."""
  total_package_length = sum(
      len(entry.get('package', {}).get('name', '')) for entry in affected)
  return total_package_length > 70 or len(affected) > 5


@blueprint.app_template_filter('group_versions')
def group_versions(versions, ecosystem):
  """Group versions by prefix."""
  groups = {}

  for version in sort_versions(versions, ecosystem):
    if '.' not in version:
      groups.setdefault('Other', []).append(version)
      continue

    label = version.split('.')[0] + '.*'
    groups.setdefault(label, []).append(version)

  return groups


def sort_versions(versions: list[str], ecosystem: str) -> list[str]:
  """Sorts a list of version numbers in the given ecosystem's sorting order."""
  try:
    return sorted(versions, key=osv.ecosystems.get(ecosystem).sort_key)
  except (NotImplementedError, AttributeError):
    # If the ecosystem doesn't support ordering,
    # the versions are sorted lexicographically.
    return sorted(versions)


@blueprint.app_template_filter('markdown')
def markdown(text):
  """Render markdown."""
  if text:
    md = markdown2.markdown(
        text, safe_mode='escape', extras=['fenced-code-blocks'])
    # TODO(michaelkedar): Seems like there's a bug with markdown2 not escaping
    # unclosed HTML comments <!--, which ends up commenting out the whole page
    # See: https://github.com/trentm/python-markdown2/issues/563
    # For now, manually replace any leftover comments with the escaped form
    return md.replace('<!--', '&lt;!--')

  return ''


@blueprint.app_template_filter('display_json')
def display_json(data):
  # We can't use the default `tojson` filter as it's intended for code (and
  # escapes characters like '<' to '\u003c'). We want to render the JSON for
  # display purposes and use HTML escaping ('&lt;') instead so it's rendered
  # as '<'.
  return json.dumps(data, indent=4)


@blueprint.app_template_filter('log')
def logarithm(n):
  return math.log(n)


@blueprint.app_template_filter('strip_scheme')
def strip_scheme(url):
  parsed_result = parse.urlparse(url)
  scheme = f"{parsed_result.scheme}://"
  return parsed_result.geturl().replace(scheme, '', 1)


@blueprint.app_template_filter('git_repo')
def git_repo(affected):
  git_repos = []
  for a in affected:
    git_repos.extend([
        r.get('repo', '')
        for r in a.get('ranges', [])
        if r.get('type', '') == 'GIT'
    ])
  return git_repos


@blueprint.app_template_filter('package_in_ecosystem')
def package_in_ecosystem(package):
  ecosystem = osv.ecosystems.normalize(package['ecosystem'])
  if ecosystem in osv.ecosystems.package_urls:
    return osv.ecosystems.package_urls[ecosystem] + package['name']
  return ''


@blueprint.app_template_filter('osv_has_vuln')
def osv_has_vuln(vuln_id):
  """Checks if an osv vulnerability exists for the given ID."""
  return osv.Bug.get_by_id(vuln_id)


@blueprint.app_template_filter('list_packages')
def list_packages(vuln_affected: list[dict]):
  """Lists all affected package names without duplicates,
  remaining in the original order."""
  packages = []

  for affected in vuln_affected:
    for affected_range in affected.get('ranges', []):
      if affected_range['type'] in ['ECOSYSTEM', 'SEMVER']:
        if 'package' not in affected:
          continue
        package_entry = affected['package']['ecosystem'] + '/' + affected[
            'package']['name']
        if package_entry not in packages:
          packages.append(package_entry)
      elif affected_range['type'] == 'GIT':
        parsed_scheme = strip_scheme(affected_range['repo'])
        if parsed_scheme not in packages:
          packages.append(parsed_scheme)

  return packages


@blueprint.app_errorhandler(404)
def not_found_error(error: exceptions.HTTPException):
  logging.info('Handled %s - Path attempted: %s', error, request.path)
  return render_template('404.html'), 404


@blueprint.app_template_filter('has_link_to_deps_dev')
def has_link_to_deps_dev(ecosystem):
  """
  Check if a given ecosystem has a corresponding link in deps.dev.

  Returns:
      bool: True if the ecosystem has a corresponding link in deps.dev,
            False otherwise.
  """
  return osv.ecosystems.is_supported_in_deps_dev(ecosystem)


@blueprint.app_template_filter('link_to_deps_dev')
def link_to_deps_dev(package, ecosystem):
  """
  Generate a link to the deps.dev page for a given package in the specified
  ecosystem.

  Args:
      package (str): The name of the package.
      ecosystem (str): The ecosystem name.
  Returns:
      str or None: The URL to the deps.dev page for the package if the
      ecosystem is supported, None otherwise.
  """
  system = osv.ecosystems.map_ecosystem_to_deps_dev(ecosystem)
  if not system:
    return None
  # This ensures that special characters such as / are properly encoded,
  # preventing invalid paths and 404 errors.
  # e.g. for the package name github.com/rancher/wrangler,
  # return https://deps.dev/go/github.com%2Francher%2Fwrangler
  encoded_package = parse.quote(package, safe='')
  return f"{_DEPS_BASE_URL}/{system}/{encoded_package}"


@blueprint.app_template_filter('display_severity_rating')
def display_severity_rating(severity: dict) -> str:
  """Return base score and rating of the severity."""
  severity_base_score, severity_rating = calculate_severity_details(severity)
  if severity_base_score is None:
    return "Invalid Severity Rating"

  return f"{severity_base_score} ({severity_rating})"


@blueprint.app_template_filter('severity_level')
def severity_level(severity: dict) -> str:
  """Return rating of the severity."""
  _, rating = calculate_severity_details(severity)
  return (rating and rating.lower()) or "invalid"


@blueprint.app_template_filter('cvss_calculator_url')
def cvss_calculator_url(severity):
  """Generate the FIRST CVSS calculator URL from a CVSS string."""
  score = severity.get('score')

  # Extract CVSS version from the vector string
  version = score.split('/')[0].split(':')[1]

  return f"{_FIRST_CVSS_CALCULATOR_BASE_URL}/{version}#{score}"


@blueprint.app_template_filter('relative_time')
def relative_time(timestamp: str) -> str:
  """Convert the input to a human-readable relative time."""
  return utils.relative_time(timestamp)
