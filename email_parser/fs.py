"""
All filesystem interaction.
"""

import logging
import os
import parse
from pathlib import Path
from string import Formatter

from . import const, config
from .model import *

logger = logging.getLogger(__name__)


def _parse_params(pattern):
    params = [p for p in map(lambda e: e[1], Formatter().parse(pattern)) if p]
    if 'name' not in params:
        raise MissingPatternParamError(
            '{{name}} is a required parameter in the pattern but it is not present in {}'.format(pattern))
    if 'locale' not in params:
        raise MissingPatternParamError(
            '{{locale}} is a required parameter in the pattern but it is not present in {}'.format(pattern))
    return params


def _has_correct_ext(path, pattern):
    return os.path.splitext(str(path))[1] == os.path.splitext(pattern)[1]


# TODO extract globals
def _emails(root_path, pattern, params):
    source_path = os.path.join(root_path, config.paths.source)
    wildcard_params = {k: '*' for k in params}
    wildcard_pattern = pattern.format(**wildcard_params)
    parser = parse.compile(pattern)
    glob_path = Path(source_path).glob(wildcard_pattern)
    for path in sorted(glob_path, key=lambda path: str(path)):
        if not path.is_dir() and _has_correct_ext(path, pattern):
            str_path = str(path.relative_to(source_path))
            result = parser.parse(str_path)
            if result:  # HACK: result can be empty when pattern doesn't contain any placeholder
                result.named['path'] = str(path.resolve())
                if not str_path.endswith(const.GLOBALS_EMAIL_NAME + const.SOURCE_EXTENSION):
                    logger.debug('loading email %s', result.named['path'])
                    yield result


def get_email_filepath(root_path, email_name, locale):
    pattern = config.pattern.replace('{name}', email_name)
    pattern = pattern.replace('{locale}', locale)
    filepath = os.path.join(config.paths.source, pattern)
    return filepath


def emails(root_path, email_name=None, locale=None):
    """
    Resolves a pattern to a collection of emails.

    :param src_dir: base dir for the search
    :param pattern: search pattern
    :exclusive_path: single email path, glob path for emails subset or None to not affect emails set

    :returns: generator for the emails matching the pattern
    """
    params = _parse_params(config.pattern)
    pattern = config.pattern
    if email_name:
        pattern = pattern.replace('{name}', email_name)
    if locale:
        pattern = pattern.replace('{locale}', locale)
    for result in _emails(root_path, pattern, params):
        if email_name:
            result.named['name'] = email_name
        if locale:
            result.named['locale'] = locale
        yield Email(**result.named)


def email(root_path, email_name, locale):
    """
    Gets an email by name and locale

    :param src_dir: base dir for the search
    :param pattern: search pattern
    :param email_name: email name
    :param locale: locale name or None for all locales

    :returns: generator for the emails with email_name
    """
    params = _parse_params(config.pattern)
    pattern = config.pattern.replace('{name}', email_name)
    pattern = pattern.replace('{locale}', locale)
    for result in _emails(root_path, pattern, params):
        result.named['name'] = email_name
        result.named['locale'] = locale
        return Email(**result.named)
    return None


def global_email(root_path, locale):
    path = os.path.join(root_path, config.paths.source, locale, const.GLOBALS_EMAIL_NAME + const.SOURCE_EXTENSION)
    return Email(const.GLOBALS_EMAIL_NAME, locale, path)


def read_file(*path_parts):
    """
    Helper for reading files
    """
    try:
        path = os.path.join(*path_parts)
    except:
        logger.error(path_parts)
    logger.debug('reading file from %s', path)
    with open(path) as fp:
        return fp.read()


def save_file(content, *path_parts):
    """
    Helper for saving files
    """
    path = os.path.join(*path_parts)
    logger.debug('saving file to %s', path)
    with open(path, 'w') as fp:
        return fp.write(content)


def delete_file(*path_parts):
    """
    Helper for deleting files
    """
    path = os.path.join(*path_parts)
    logger.debug('deleting file to %s', path)
    os.remove(path)


def save_email(root_path, content, email_name, locale):
    pattern = config.pattern.replace('{locale}', locale)
    pattern = pattern.replace('{name}', email_name)
    path = os.path.join(root_path, config.paths.source, pattern)
    save_file(content, path)
    return path


def save_parsed_email(root_path, email, subject, text, html):
    """
    Saves an email. The locale and name are taken from email tuple.

    :param email: Email tuple
    :param subject: email's subject
    :param text: email's body as text
    :param html: email's body as html
    :param dest_dir: root destination directory
    """
    locale = email.locale or const.DEFAULT_LOCALE
    folder = os.path.join(root_path, config.paths.destination, locale)
    os.makedirs(folder, exist_ok=True)
    save_file(subject, folder, email.name + const.SUBJECT_EXTENSION)
    save_file(text, folder, email.name + const.TEXT_EXTENSION)
    save_file(html, folder, email.name + const.HTML_EXTENSION)


def resources(root_path):
    """
    Returns a tuple of lists: html templates list and css styles list
    :param root_path:
    :return:
    """
    templates = {}
    styles = []
    templates_path = os.path.join(root_path, config.paths.templates)
    glob_path = Path(templates_path).glob('**/*')
    paths = sorted(glob_path, key=lambda p: str(p))
    css_files = filter(lambda p: p.is_file() and p.name.endswith(const.CSS_EXTENSION), paths)
    styles.extend(map(lambda p: p.name, css_files))
    html_files = filter(lambda p: p.is_file() and p.name.endswith(const.HTML_EXTENSION), paths)
    for html_file in html_files:
        parent = html_file.relative_to(templates_path).parent
        template_type = None if str(parent) == '.' else str(parent)
        templates_list_by_type = templates.setdefault(template_type, [])
        templates_list_by_type.append(html_file.name)
    return templates, styles
