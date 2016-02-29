import re
import bs4
import os, os.path
from .. import fs
from ..renderer import HtmlRenderer
from ..reader import Template, read as reader_read
import fnmatch
import cherrypy
from functools import lru_cache
from collections import namedtuple
import random, string
import urllib.parse
import time
from html import escape as html_escape
import jinja2
import pkgutil


RESOURCE_PACKAGE = 'email_parser.resources.gui'


DOCUMENT_TIMEOUT = 24 * 60 * 60  # 24 hours

HTML_PARSER = 'lxml'

STYLES_PARAM_NAME = 'HIDDEN__styles'
TEMPLATE_PARAM_NAME = 'HIDDEN__template'
EMAIL_PARAM_NAME = 'HIDDEN__saved_email_filename'
WORKING_PARAM_NAME = 'HIDDEN__working_name'
LAST_ACCESS_PARAM_NAME = 'HIDDEN__last_access_time'
FINAL_INCOMPLETE_CODE = 'HIDDEN__preview_incomplete_code'

OVERWRITE_PARAM_NAME = 'overwrite'
SAVEAS_PARAM_NAME = 'saveas_filename'

CONTENT_TYPES = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
}


# Dealing with documents, in our cache & invented from POST params


Document = namedtuple('Document', ['working_name', 'email_name', 'template_name', 'styles', 'args'])


RECENT_DOCUMENTS = dict()


def _clean_documents():
    expired_keys = set()
    for key, value in RECENT_DOCUMENTS.items():
        last_access = value.setdefault(LAST_ACCESS_PARAM_NAME, time.time())
        if last_access < time.time() - DOCUMENT_TIMEOUT:
            expired_keys.add(key)
    for key in expired_keys:
        del RECENT_DOCUMENTS[key]


@lru_cache(maxsize=64)
def _get_working_args(working_name):
    result = RECENT_DOCUMENTS.get(working_name, {WORKING_PARAM_NAME: working_name})
    RECENT_DOCUMENTS[working_name] = result
    return result


def _new_working_args():
    working_name = ''.join(random.SystemRandom().choice(string.ascii_letters + string.digits) for _ in range(8))
    _clean_documents()
    return _get_working_args(working_name)


def _extract_document(args=None, working_name=None, email_name=None, template_name=None, template_styles=None):
    # Return working_name, email_name, template_name, styles, args
    args = args or dict()
    working_name = working_name or args.pop(WORKING_PARAM_NAME, working_name)
    if working_name:
        working_args = _get_working_args(working_name)
    else:
        working_args = _new_working_args()
    working_args.update(args)
    working_args[LAST_ACCESS_PARAM_NAME] = time.time()
    if email_name:
        working_args.update({EMAIL_PARAM_NAME: email_name})
    if template_name:
        working_args.update({TEMPLATE_PARAM_NAME: template_name})

    result_args = dict(working_args)
    working_name = result_args.pop(WORKING_PARAM_NAME)
    email_name = result_args.pop(EMAIL_PARAM_NAME, None)
    template_name = result_args.pop(TEMPLATE_PARAM_NAME, None)
    styles = template_styles or _pop_styles(result_args) or []
    working_args.update({STYLES_PARAM_NAME: ','.join(styles)})
    return Document(working_name, email_name, template_name, styles, result_args)


# Dealing with HTML & soup


def _get_body_content_string(soup, comments=True):
    if not isinstance(soup, bs4.BeautifulSoup):
        soup = bs4.BeautifulSoup(soup, HTML_PARSER)
    return ''.join(
        ('<!--{}-->'.format(C) if comments else '') if isinstance(C, bs4.Comment)
        else str(C)
        for C in soup.body.contents
    )


# Utilities


def _list_files_recursively(path, hidden=False, relative_to_path=False):
    result = set()
    for dirpath, _, filenames in os.walk(path):
        if hidden or not dirpath.startswith('.'):
            for filename in filenames:
                if hidden or not filename.startswith('.'):
                    name = os.path.join(dirpath, filename)
                    if relative_to_path:
                        name = os.path.relpath(name, path)
                    result.add(name)
    return sorted(result)


def _pop_styles(args):
    styles = args.pop(STYLES_PARAM_NAME, [])
    if isinstance(styles, str):
        styles = styles.split(',')
    styles = [S for S in styles if S.lower().endswith('.css')]
    return styles


def _unplaceholder(placeholders):
    def fix_item(item):
        if item.startswith('[[') and item.endswith(']]'):
            return item[2:-2]
        else:
            return item
    return {K: fix_item(V) for K, V in placeholders.items()}


# Editing & rendering


class InlineFormReplacer(object):
    """
    Reads template files & inserts values to approximate rendered format (or editor-style format)
    """
    # Group 1: spaces and preceding non-space character: must be returned with replacement
    # Group 2: preceding non-space character (if any)
    # Group 3: replace tag
    # Group 4: lookahead: following non-space character (if any)
    CONTENT_REGEX = re.compile(r'(([">]?)[^">{}]*)\{\{\s*(\w+)\s*\}\}(?=[^"<{}]*(["<]?))')

    def __init__(self, builtins=None, values=None):
        self.builtins = builtins or dict()
        self.values = values or dict()
        self.names = list()
        self.attrs = list()
        self.required = list()  # Only valid after we've done a replacement on a template

    def require(self, name):
        if name not in self.names:
            self.names.append(name)
        if self.values.get(name):
            return self.values[name]
        elif self.builtins.get(name):
            return self.builtins[name]
        else:
            self.required.append(name)
            return ''

    def _sub(self, match):
        before, prefix, name, postfix = match.groups()

        self.names.append(name)

        if name in self.builtins:
            return before + self.builtins[name]
        elif prefix == '>' or postfix == '<':
            return before + self._textarea(name)
        elif '"' in (prefix, postfix):
            self.attrs.append(name)
            return before + (self.require(name) or ('{{' + name + '}}'))
        else:
            return before + self._textarea(name)

    def _textarea(self, name):
        value = self.require(name)
        return ('<textarea class="{2}" name="{0}" placeholder="{0}"' +
                ' style="resize: vertical; width: 95%; height: 160px;">{1}</textarea>'
                ).format(name, value, 'present' if value else 'absent')

    def replace(self, template_html):
        return self.CONTENT_REGEX.sub(self._sub, template_html)

    def _should_make_placeholder(self, key):
        return key in self.names and key not in self.builtins

    def _format_placeholder(self, key, value):
        return '[[{0}]]'.format(value) if key in self.attrs else value

    def placeholders(self, fill_missing=None):
        """
        Generate placeholder dict.
        :param fill_missing: A function taking a missing placeholder name & returning a temporary fill value.
                May be `None` indicating not to return such replacements.
        :return: Dict of placeholder names to values.
        """
        result = {
            K: self._format_placeholder(K, V)
            for K, V in self.values.items()
            if self._should_make_placeholder(K)
        }
        print('Making result!')
        if fill_missing is not None:
            for key in self.required:
                print('Checking out {}'.format(key))
                if self._should_make_placeholder(key) and not result.get(key):
                    result[key] = self._format_placeholder(key, fill_missing(key))
                    print('Added!', key, result[key])
        return result

    def make_value_list(self):
        values = list()
        written_names = set()
        for name in self.names:
            if name in written_names or name in self.builtins:
                continue
            written_names.add(name)
            value = self.require(name)
            values.append([name, value])
        return values


class GenericRenderer(object):
    """
    Render directories and simple documents.
    """
    def __init__(self, settings):
        self.settings = settings
        self._resource_cache = dict()

    def resource(self, resource_name):
        resource = self._resource_cache.get(resource_name)
        if resource is None:
            resource = pkgutil.get_data(RESOURCE_PACKAGE, resource_name).decode('utf-8')
            # resource = fs.read_file(self.resources, resource_name)
            self._resource_cache[resource_name] = resource
        return resource

    def gui_template(self, template_name, **args):
        resource = self.resource(template_name)
        return jinja2.Template(resource).render(**args)

    def directory(
            self,
            description, root, path, href,
            accepts=(lambda path: not os.path.basename(path).startswith('.')),
            old_filename='',
            actions=()
    ):
        root_path = os.path.join(root, path)
        parent = href(os.path.dirname(path)) if path else None
        files = list()
        for name in sorted(os.listdir(root_path)):
            name_path = os.path.join(path, name)
            if accepts(name):
                if os.path.isdir(os.path.join(root, path, name)):
                    files.append([href(name_path), "\U0001f4c2", name])
                else:
                    files.append([href(name_path), "\U0001f4c4", name])
        return self.gui_template(
            'directory.html.jinja2',
            title=description,
            old_filename=old_filename,
            parent=parent,
            actions=actions,
            files=files,
        )

    def question(self, title, description, actions):
        return self.gui_template(
            'question.html.jinja2',
            title=title,
            description=description,
            actions=actions,
        )


class InlineFormRenderer(GenericRenderer):
    """
    Render editors and previews
    """
    def __init__(self, settings):
        super().__init__(settings)
        self.settings = settings

    def save(self, email_name, template_name, styles, **args):
        """
        Saves email in XML format
        :param email_name:
        :param template_name:
        :param styles:
        :param args:
        :return:
        """
        replacer, _ = self._make_replacer(args, template_name)
        xml = self.gui_template(
            'email.xml.jinja2',
            template=template_name,
            style=','.join(styles),
            attrs=replacer.attrs,
            values=replacer.make_value_list(),
        )

        fs.save_file(xml, self.settings.source, email_name)

    def render_preview(self, template_name, styles, **args):
        replacer, _ = self._make_replacer(args, template_name)
        if styles:
            # Use "real" renderer, replace missing values with ???
            placeholders = replacer.placeholders(lambda missing_key: '???')
            html = HtmlRenderer(Template(template_name, styles), self.settings, '').render(placeholders)
        else:
            html = self.resource('preview.no.styles.html')
        return html, (styles and not replacer.required)

    def render(
            self, template_name, styles=(),
            actions={},
            **args
    ):
        replacer, html = self._make_replacer(args, template_name)

        edit_html = html
        edit_column = _get_body_content_string(edit_html).strip()
        image_attrs = self._find_image_attrs(edit_html)
        image_filenames = self._find_images()

        return self.gui_template(
            "editor.html.jinja2",
            view_url=actions.get('preview_fragment'),
            title='Editing {}'.format(template_name),
            subject=args.get('subject', ''),
            content=edit_column,
            all_styles=self._find_styles(),
            styles=styles,
            save_url=actions.get('save'),
            attrs=replacer.attrs,
            values=args,
            dropdowns={A: image_filenames for A in image_attrs},
        )

    def _read_template(self, template_name):
        return fs.read_file(self.settings.templates, template_name)

    def _find_styles(self, path_glob='*.css'):
        return list(fnmatch.filter(os.listdir(self.settings.templates), path_glob))

    def _find_images(self, local_dir=None):
        if local_dir is None:
            local_dir = os.path.join(self.settings.templates, 'img')
        return _list_files_recursively(local_dir, relative_to_path=True)

    def _find_image_attrs(self, html):
        base_url = self.settings.images
        soup = bs4.BeautifulSoup(html, HTML_PARSER)
        pattern = re.compile('^.*\{\{(.*)\}\}.*$')

        attrs = set()
        for image in soup.find_all(
                'img',
                attrs={
                    'src': (lambda x: x.startswith(base_url) and pattern.match(x))
                }
        ):
            src = image.get('src')
            attr = pattern.match(src).group(1).strip()
            attrs.add(attr)
        return attrs

    def _make_replacer(self, args, template_name):
        replacer = InlineFormReplacer({'base_url': self.settings.images}, args)
        replacer.require('subject')
        # Generate our filled-in template
        template_html = self._read_template(template_name)
        html = replacer.replace(template_html)
        return replacer, html


# Server


class Server(object):
    def __init__(self, settings, renderer):
        self.settings = settings
        self.renderer = renderer

    @classmethod
    def _actions(cls, document, **args):
        qargs = '?' + urllib.parse.urlencode(args) if args else ''
        return {
            'preview': '/preview/{}{}'.format(document.working_name, qargs),
            'save': '{}{}'.format(cls._make_save_url(document), qargs),
            'edit': '/edit/{}{}'.format(document.working_name, qargs),
            'preview_fragment': '/preview_fragment/{}{}'.format(document.working_name, qargs),
        }

    @cherrypy.expose
    def img(self, *path):
        img_name = os.path.join(*path) if path else ''
        img_path = os.path.join(self.settings.images, *path)
        if os.path.isdir(img_path):
            return self.renderer.directory(
                'Contents of ' + (img_name or 'image directory'),
                self.settings.images, img_name,
                '/img/{}'.format,
            )
        else:
            _, ext = os.path.splitext(os.path.join(*path))
            content_type = CONTENT_TYPES.get(
                ext.lower(),
                'image/{}'.format(ext[1:].lower())
            )

            data = fs.read_file(self.settings.images, *path, mode='rb')
            cherrypy.response.headers['Content-Type'] = content_type
            return data

    @cherrypy.expose
    def index(self):
        return self.renderer.question(
            'KS-Email-Parser GUI',
            'Do you want to create a new email from a template, or edit an existing email?',
            [
                    ['Create new', '/template'],
                    ['Edit', '/email'],
            ]
        )

    @cherrypy.expose
    def timeout(self, *_ignored, **_also_ignored):
        return self.renderer.question(
            '\U0001f62d SORRY \U0001f62d',
            'Your session has timed out! Do you want to create a new email from a template, or edit an existing email?',
            [
                    ['Create new', '/template'],
                    ['Edit', '/email'],
            ]
        )

    @cherrypy.expose
    def template(self, *paths, **_ignored):
        template_name = '/'.join(paths)
        template_path = os.path.join(self.settings.templates, template_name)
        if os.path.isdir(template_path):
            return self.renderer.directory(
                'Contents of ' + (template_name or 'template directory'),
                self.settings.templates, template_name,
                '/template/{}'.format,
                (lambda path: os.path.isdir(path) or '.htm' in path.lower())
            )
        else:  # A file
            document = _extract_document({}, template_name=template_name)
            if not document.template_name:
                raise cherrypy.HTTPRedirect('/timeout')
            return self.renderer.render(
                document.template_name,
                document.styles,
                actions=self._actions(document, **{TEMPLATE_PARAM_NAME: template_name}),
            )

    @cherrypy.expose
    def preview(self, working_name, **args):
        document = _extract_document(args, working_name)
        print(document)
        if not document.template_name:
            raise cherrypy.HTTPRedirect('/timeout')

        html, _ = self.renderer.render_preview(
            document.template_name,
            document.styles,
            **document.args
        )
        return html

    @cherrypy.expose
    def preview_fragment(self, working_name, **args):
        document = _extract_document(args, working_name)
        print(document)
        if not document.template_name:
            raise cherrypy.HTTPRedirect('/timeout')

        html, is_complete = self.renderer.render_preview(
            document.template_name,
            document.styles,
            **document.args
        )
        fragment = _get_body_content_string(html).strip()
        if not is_complete:
            # Hack so client can tell from result that it's incomplete
            fragment += ' <!-- {} -->'.format(FINAL_INCOMPLETE_CODE)
        return fragment

    @cherrypy.expose
    def edit(self, working_name, **args):
        document = _extract_document(args, working_name)
        if not document.template_name:
            raise cherrypy.HTTPRedirect('/timeout')

        return self.renderer.render(
            document.template_name,
            document.styles,
            actions=self._actions(document),
            **document.args
        )

    @cherrypy.expose
    def email(self, *paths, **_ignored):
        email_name = '/'.join(paths)
        email_path = os.path.join(self.settings.source, email_name)
        if os.path.isdir(email_path):
            return self.renderer.directory(
                'Contents of ' + (email_name or 'source directory'),
                self.settings.source, email_name,
                '/email/{}'.format
            )
        else:  # A file
            template, placeholders, _ = reader_read(email_path)
            args = _unplaceholder(placeholders)

            html = HtmlRenderer(template, self.settings, '').render(placeholders)
            return self.renderer.question(
                title=html_escape(args.get('subject')),
                description=_get_body_content_string(html),
                actions=[
                    ['Edit', '/alter/{}'.format(email_name)],
                ]
            )

    @cherrypy.expose
    def alter(self, *paths, **_ignored):
        email_name = '/'.join(paths)
        email_path = os.path.join(self.settings.source, email_name)
        template, placeholders, _ = reader_read(email_path)
        args = _unplaceholder(placeholders)
        document = _extract_document(
            args,
            email_name=email_name,
            template_name=template.name,
            template_styles=template.styles
        )

        return self.renderer.render(
            document.template_name,
            document.styles,
            actions=self._actions(document, **{EMAIL_PARAM_NAME: email_name}),
            **document.args
        )

    @cherrypy.expose
    def saveas(self, working_name, *email_paths, **args):
        email_name = '/'.join(email_paths)
        saveas = args.pop(SAVEAS_PARAM_NAME, None)
        if saveas:
            email_name = '/'.join((email_name, saveas))
        raise cherrypy.HTTPRedirect('/save/{0}/{1}'.format(working_name, email_name))

    @cherrypy.expose
    def save(self, working_name, *paths, **args):
        rel_path = '/'.join(paths)
        full_path = os.path.join(self.settings.source, rel_path)
        document = _extract_document({}, working_name)
        if not document.template_name:
            raise cherrypy.HTTPRedirect('/timeout')

        overwrite = args.pop(OVERWRITE_PARAM_NAME, False)
        if overwrite or not os.path.exists(full_path):
            # Create and save
            self.renderer.save(rel_path, document.template_name, document.styles, **document.args)
            raise cherrypy.HTTPRedirect('/email/{}'.format(rel_path))
        elif os.path.isdir(full_path):
            # Show directory or allow user to create new file
            html = self.renderer.directory(
                'Select save name/directory: ' + rel_path,
                self.settings.source, rel_path,
                (lambda path: '/save/{0}/{1}'.format(working_name, path)),
                old_filename=os.path.basename(document.email_name) if document.email_name else '',
                actions=[
                    ['Save', '/saveas/{0}/{1}'.format(working_name, rel_path)],
                    ['Return to Edit', '/edit/{}'.format(working_name)],
                ]
            )
            return html
        else:
            # File already exists: overwrite?
            return self.renderer.question(
                'Overwriting ' + rel_path,
                'Are you sure you want to overwrite the existing email <code>{}</code>?'.format(rel_path),
                [
                    ['No, save as a new file',
                     '/save/{0}/{1}'.format(
                         working_name, os.path.dirname(rel_path)
                     )],
                    ['Yes, how dare you question me!',
                     '/save/{0}/{1}?{2}=1'.format(
                         working_name, rel_path, OVERWRITE_PARAM_NAME
                     )],
                ]
            )

    @classmethod
    def _make_save_url(cls, document):
        if document.email_name:
            return '/save/{}/{}'.format(document.working_name, document.email_name)
        else:
            return '/save/{}'.format(document.working_name)


def serve(args):
    from ..cmd import read_settings
    settings = read_settings(args)

    renderer = InlineFormRenderer(settings)
    cherrypy.config.update({'server.socket_port': args.port or 8080})
    cherrypy.quickstart(Server(settings, renderer), '/')