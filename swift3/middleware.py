# Copyright (c) 2010 OpenStack, LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
The swift3 middleware will emulate the S3 REST api on top of swift.

The following opperations are currently supported:

    * GET Service
    * DELETE Bucket
    * GET Bucket (List Objects)
    * PUT Bucket
    * DELETE Object
    * GET Object
    * HEAD Object
    * PUT Object
    * PUT Object (Copy)

To add this middleware to your configuration, add the swift3 middleware
in front of the auth middleware, and before any other middleware that
look at swift requests (like rate limiting).

To set up your client, the access key will be the concatenation of the
account and user strings that should look like test:tester, and the
secret access key is the account password.  The host should also point
to the swift storage hostname.  It also will have to use the old style
calling format, and not the hostname based container format.

An example client using the python boto library might look like the
following for an SAIO setup::

    connection = boto.s3.Connection(
        aws_access_key_id='test:tester',
        aws_secret_access_key='testing',
        port=8080,
        host='127.0.0.1',
        is_secure=False,
        calling_format=boto.s3.connection.OrdinaryCallingFormat())
"""

from urllib import unquote, quote
import base64
from xml.sax.saxutils import escape as xml_escape
import urlparse
from xml.dom.minidom import parseString

from simplejson import loads
import email.utils
import datetime
import re

from swift.common.utils import split_path
from swift.common.utils import get_logger
from swift.common.wsgi import WSGIContext
from swift.common.middleware.acl import parse_acl
from swift.obj import server as obj_server
from swift.container import server as container_server
from swift.common.http import HTTP_OK, HTTP_CREATED, HTTP_ACCEPTED, \
    HTTP_NO_CONTENT, HTTP_BAD_REQUEST, HTTP_UNAUTHORIZED, HTTP_FORBIDDEN, \
    HTTP_NOT_FOUND, HTTP_CONFLICT, HTTP_UNPROCESSABLE_ENTITY, is_success, \
    HTTP_NOT_IMPLEMENTED, HTTP_LENGTH_REQUIRED
from swift.common.middleware.s3acl import AUTHENTICATED_USERNAME

try:
    from swift.common.swob import Request, Response
except ImportError:
    from webob import Request, Response


MAX_BUCKET_LISTING = 1000
AMZ_ALL_USERS = 'http://acs.amazonaws.com/groups/global/AllUsers'
AMZ_AUTHENTICATED_USERS =\
    'http://acs.amazonaws.com/groups/global/AuthenticatedUsers'
REPLACE_USERNAMES = {
    AMZ_ALL_USERS: '.r:*',
    AMZ_AUTHENTICATED_USERS: AUTHENTICATED_USERNAME
}


def get_err_response(code):
    """
    Given an HTTP response code, create a properly formatted xml error response

    :param code: error code
    :returns: webob.response object
    """
    error_table = {
        'AccessDenied':
        (HTTP_FORBIDDEN, 'Access denied'),
        'BucketAlreadyExists':
        (HTTP_CONFLICT, 'The requested bucket name is not available'),
        'BucketNotEmpty':
        (HTTP_CONFLICT, 'The bucket you tried to delete is not empty'),
        'InvalidArgument':
        (HTTP_BAD_REQUEST, 'Invalid Argument'),
        'InvalidBucketName':
        (HTTP_BAD_REQUEST, 'The specified bucket is not valid'),
        'InvalidURI':
        (HTTP_BAD_REQUEST, 'Could not parse the specified URI'),
        'InvalidDigest':
        (HTTP_BAD_REQUEST, 'The Content-MD5 you specified was invalid'),
        'BadDigest':
        (HTTP_BAD_REQUEST, 'The Content-Length you specified was invalid'),
        'NoSuchBucket':
        (HTTP_NOT_FOUND, 'The specified bucket does not exist'),
        'SignatureDoesNotMatch':
        (HTTP_FORBIDDEN, 'The calculated request signature does not '
            'match your provided one'),
        'RequestTimeTooSkewed':
        (HTTP_FORBIDDEN, 'The difference between the request time and the'
        ' current time is too large'),
        'NoSuchKey':
        (HTTP_NOT_FOUND, 'The resource you requested does not exist'),
        'Unsupported':
        (HTTP_NOT_IMPLEMENTED, 'The feature you requested is not yet'
        ' implemented'),
        'MissingContentLength':
        (HTTP_LENGTH_REQUIRED, 'Length Required'),
        'IllegalVersioningConfigurationException':
            (HTTP_BAD_REQUEST, 'The specified versioning configuration '
                               'invalid'),
        'MalformedACLError':
            (HTTP_BAD_REQUEST, 'The XML you provided was not well-formed or '
                               'did not validate against our published schema')
    }

    resp = Response(content_type='text/xml')
    resp.status = error_table[code][0]
    resp.body = error_table[code][1]
    resp.body = '<?xml version="1.0" encoding="UTF-8"?>\r\n<Error>\r\n  ' \
                '<Code>%s</Code>\r\n  <Message>%s</Message>\r\n</Error>\r\n' \
                % (code, error_table[code][1])
    return resp


def get_acl(account_name, headers):
    """
    Attempts to construct an S3 ACL based on what is found in the swift headers
    """

    acl = 'private'  # default to private

    if 'x-container-read' in headers:
        if headers['x-container-read'] == ".r:*" or\
            ".r:*," in headers['x-container-read'] or \
                ",*," in headers['x-container-read']:
            acl = 'public-read'
    if 'x-container-write' in headers:
        if headers['x-container-write'] == ".r:*" or\
            ".r:*," in headers['x-container-write'] or \
                ",*," in headers['x-container-write']:
            if acl == 'public-read':
                acl = 'public-read-write'
            else:
                acl = 'public-write'

    if acl == 'private':
        body = ('<AccessControlPolicy>'
                '<Owner>'
                '<ID>%s</ID>'
                '<DisplayName>%s</DisplayName>'
                '</Owner>'
                '<AccessControlList>'
                '<Grant>'
                '<Grantee xmlns:xsi="http://www.w3.org/2001/'
                'XMLSchema-instance" xsi:type="CanonicalUser">'
                '<ID>%s</ID>'
                '<DisplayName>%s</DisplayName>'
                '</Grantee>'
                '<Permission>FULL_CONTROL</Permission>'
                '</Grant>'
                '</AccessControlList>'
                '</AccessControlPolicy>' %
                (account_name, account_name, account_name, account_name))
    elif acl == 'public-read':
        body = ('<AccessControlPolicy>'
                '<Owner>'
                '<ID>%s</ID>'
                '<DisplayName>%s</DisplayName>'
                '</Owner>'
                '<AccessControlList>'
                '<Grant>'
                '<Grantee xmlns:xsi="http://www.w3.org/2001/'
                'XMLSchema-instance" xsi:type="CanonicalUser">'
                '<ID>%s</ID>'
                '<DisplayName>%s</DisplayName>'
                '</Grantee>'
                '<Permission>FULL_CONTROL</Permission>'
                '</Grant>'
                '<Grant>'
                '<Grantee xmlns:xsi="http://www.w3.org/2001/'
                'XMLSchema-instance" xsi:type="Group">'
                '<URI>http://acs.amazonaws.com/groups/global/AllUsers</URI>'
                '</Grantee>'
                '<Permission>READ</Permission>'
                '</Grant>'
                '</AccessControlList>'
                '</AccessControlPolicy>' %
                (account_name, account_name, account_name, account_name))
    elif acl == 'public-read-write':
        body = ('<AccessControlPolicy>'
                '<Owner>'
                '<ID>%s</ID>'
                '<DisplayName>%s</DisplayName>'
                '</Owner>'
                '<AccessControlList>'
                '<Grant>'
                '<Grantee xmlns:xsi="http://www.w3.org/2001/'
                'XMLSchema-instance" xsi:type="CanonicalUser">'
                '<ID>%s</ID>'
                '<DisplayName>%s</DisplayName>'
                '</Grantee>'
                '<Permission>FULL_CONTROL</Permission>'
                '</Grant>'
                '<Grant>'
                '<Grantee xmlns:xsi="http://www.w3.org/2001/'
                'XMLSchema-instance" xsi:type="Group">'
                '<URI>http://acs.amazonaws.com/groups/global/AllUsers</URI>'
                '</Grantee>'
                '<Permission>READ</Permission>'
                '</Grant>'
                '</AccessControlList>'
                '<AccessControlList>'
                '<Grant>'
                '<Grantee xmlns:xsi="http://www.w3.org/2001/'
                'XMLSchema-instance" xsi:type="Group">'
                '<URI>http://acs.amazonaws.com/groups/global/AllUsers</URI>'
                '</Grantee>'
                '<Permission>WRITE</Permission>'
                '</Grant>'
                '</AccessControlList>'
                '</AccessControlPolicy>' %
                (account_name, account_name, account_name, account_name))
    else:
        body = ('<AccessControlPolicy>'
                '<Owner>'
                '<ID>%s</ID>'
                '<DisplayName>%s</DisplayName>'
                '</Owner>'
                '<AccessControlList>'
                '<Grant>'
                '<Grantee xmlns:xsi="http://www.w3.org/2001/'
                'XMLSchema-instance" xsi:type="CanonicalUser">'
                '<ID>%s</ID>'
                '<DisplayName>%s</DisplayName>'
                '</Grantee>'
                '<Permission>FULL_CONTROL</Permission>'
                '</Grant>'
                '</AccessControlList>'
                '</AccessControlPolicy>' %
                (account_name, account_name, account_name, account_name))
    return Response(body=body, content_type="text/plain")


def amz_group_grant(uri, permission):
    """
    Returns XML Grant for group with URI.

    :param uri: group URI
    :param permission: permission value
    """
    grant = (
        '<Grant>'
            '<Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                    'xsi:type="Group">'
                '<URI>%s</URI>'
            '</Grantee>'
            '<Permission>%s</Permission>'
        '</Grant>' % (uri, permission))
    return grant


def amz_user_grant(id, name, permission):
    """
    Returns XML Grant for user.

    :param id: user id
    :param name: user name
    :param permission: permission value
    """
    grant = (
        '<Grant>'
            '<Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                    'xsi:type="CanonicalUser">'
                '<ID>%s</ID>'
                '<DisplayName>%s</DisplayName>'
            '</Grantee>'
            '<Permission>%s</Permission>'
        '</Grant>' % (id, name, permission))
    return grant


def get_s3_acl(headers, acl_headers, resource='container'):
    out = ['<AccessControlPolicy>']
    owner_header = 'x-%s-owner' % resource
    if owner_header in headers:
        owner = xml_escape(headers[owner_header])
        out.append('<Owner><ID>%s</ID><DisplayName>%s</DisplayName></Owner>' %
                   (owner, owner))
    out.append('<AccessControlList>')
    for header in acl_headers:
        if header in headers:
            permission = None
            if resource == 'container':
                # len(x-container-acl-) = 16; len(x-container-) = 12
                frm = 16 if header.startswith('x-container-acl-') else 12
                permission = header[frm:].upper().replace('-', '_')
            elif resource == 'object':
                # len(x-object-acl-) = 13
                permission = header[13:].upper().replace('-', '_')
            if permission:
                referrers, groups = parse_acl(headers[header])
                for ref in referrers:
                    uri = AMZ_ALL_USERS if ref == '*' else ref
                    grant = amz_group_grant(uri, permission)
                    out.append(grant)
                for group in groups:
                    grant = amz_user_grant(group, group, permission)
                    out.append(grant)
    out.append('</AccessControlList></AccessControlPolicy>')
    return Response(body=''.join(out), content_type='application/xml')


def parse_access_control_policy(xml):
    """
    Parse given access control policy XML. Sample ACP XML

    <AccessControlPolicy>
      <Owner>
        <ID>ID</ID>
        <DisplayName>EmailAddress</DisplayName>
      </Owner>
      <AccessControlList>
        <Grant>
          <Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                xsi:type="CanonicalUser">
            <ID>ID</ID>
            <DisplayName>EmailAddress</DisplayName>
          </Grantee>
          <Permission>Permission</Permission>
        </Grant>
      </AccessControlList>
    </AccessControlPolicy>

    :param xml: XML string with ACP
    :returns : dict with ACL 'owner':<owner>, 'acl': [{'user':<username>,
               'permissions':[<permission>,...]},...]}
    """
    out = {'owner': '', 'acl': []}
    dom = parseString(xml)
    if len(dom.childNodes) and \
            dom.childNodes[0].nodeName == 'AccessControlPolicy':
        acp = dom.childNodes[0]
        acl = None
        for node in acp.childNodes:
            if node.nodeName == 'Owner':
                for n in node.childNodes:
                    if n.nodeName == 'ID':
                        out['owner'] = n.childNodes[0].nodeValue
            elif node.nodeName == 'AccessControlList':
                acl = node
        if acl:
            for grant in acl.childNodes:
                user = {'user': '', 'permissions': []}
                if grant.nodeName != 'Grant':
                    continue
                for node in grant.childNodes:
                    if node.nodeName == 'Grantee':
                        for n in node.childNodes:
                            if n.nodeName == 'ID':
                                user['user'] = n.childNodes[0].nodeValue
                            elif n.nodeName == 'URI':
                                user['user'] = n.childNodes[0].nodeValue
                            elif n.nodeName == 'EmailAddress':
                                user['user'] = n.childNodes[0].nodeValue
                    elif node.nodeName == 'Permission':
                        if node.childNodes[0].nodeValue:
                            v = node.childNodes[0].nodeValue
                            user['permissions'].append(v)
                out['acl'].append(user)
    return out


def acp_to_headers(env, resource):
    """
    Update env, add Swift ACL headers based on request body (wsgi.input).

    :param env: WSGI enviroment dict
    :param resource: resource type object or container
    :returns : if any error occur return webob.Response object, else None
    """
    if 'wsgi.input' not in env:
        return get_err_response('MalformedACLError')
    try:
        acp = parse_access_control_policy(env['wsgi.input'].read())
    except:
        return get_err_response('MalformedACLError')
    if resource == 'object':
        permissions = {'HTTP_X_OBJECT_ACL_READ': [],
                       'HTTP_X_OBJECT_ACL_WRITE': [],
                       'HTTP_X_OBJECT_ACL_READ_ACP': [],
                       'HTTP_X_OBJECT_ACL_WRITE_ACP': []}
    elif resource == 'container':
        permissions = {'HTTP_X_CONTAINER_READ': [],
                       'HTTP_X_CONTAINER_ACL_READ': [],
                       'HTTP_X_CONTAINER_WRITE': [],
                       'HTTP_X_CONTAINER_ACL_READ_ACP': [],
                       'HTTP_X_CONTAINER_ACL_WRITE_ACP': []}
    for user in acp.get('acl', []):
        username = user.get('user')
        if username:
            perms = user.get('permissions', [])
            if 'FULL_CONTROL' in perms:
                perms = ['READ', 'WRITE', 'READ_ACP', 'WRITE_ACP']
            for permission in perms:
                if resource == 'object':
                    key = 'HTTP_X_OBJECT_ACL_' + permission
                elif resource == 'container':
                    if permission == 'WRITE':
                        key = 'HTTP_X_CONTAINER_' + permission
                    else:
                        key = 'HTTP_X_CONTAINER_ACL_' + permission
                if key not in permissions:
                    permissions[key] = []
                if username in REPLACE_USERNAMES:
                    username = REPLACE_USERNAMES[username]
                if username not in permissions[key]:
                    permissions[key].append(username)
    for key, value in permissions.iteritems():
        env[key] = ','.join(value)


def canonical_string(req):
    """
    Canonicalize a request to a token that can be signed.
    """
    amz_headers = {}

    buf = "%s\n%s\n%s\n" % (req.method, req.headers.get('Content-MD5', ''),
                            req.headers.get('Content-Type') or '')

    for amz_header in sorted((key.lower() for key in req.headers
                              if key.lower().startswith('x-amz-'))):
        amz_headers[amz_header] = req.headers[amz_header]

    if 'x-amz-date' in amz_headers:
        buf += "\n"
    elif 'Date' in req.headers:
        buf += "%s\n" % req.headers['Date']

    for k in sorted(key.lower() for key in amz_headers):
        buf += "%s:%s\n" % (k, amz_headers[k])

    path = req.path_qs
    args = ''
    if '?' in path:
        path, args = path.split('?', 1)
    segs = path.split('/')
    if len(segs) > 2 and segs[2]:  # segs[2] is object name
        # We doing this for replace '/' with %2F, because by default quote
        # don't replace '/' with %2F
        object_name = quote(unquote('/'.join(segs[2:])), safe='')
        path = '/'.join(segs[:2] + [object_name])
    params = []
    for key, value in urlparse.parse_qsl(args, keep_blank_values=True):
        # list of keys must be lexicographically sorted
        if key in ('acl', 'location', 'logging', 'requestPayment',
                   'torrent', 'versionId', 'versioning', 'versions'):
            params.append('%s=%s' % (key, value) if value else key)
    if params:
        return "%s%s?%s" % (buf, path, '&'.join(params))
    return buf + path


def swift_acl_translate(acl, group='', user='', xml=False):
    """
    Takes an S3 style ACL and returns a list of header/value pairs that
    implement that ACL in Swift, or "Unsupported" if there isn't a way to do
    that yet.
    """
    swift_acl = {}
    swift_acl['public-read'] = [['HTTP_X_CONTAINER_READ', '.r:*,.rlistings']]
    # Swift does not support public write:
    # https://answers.launchpad.net/swift/+question/169541
    swift_acl['public-read-write'] = [['HTTP_X_CONTAINER_WRITE', '.r:*'],
                                      ['HTTP_X_CONTAINER_READ',
                                       '.r:*,.rlistings']]

    #TODO: if there's a way to get group and user, this should work for
    # private:
    #swift_acl['private'] = [['HTTP_X_CONTAINER_WRITE',  group + ':' + user], \
    #                  ['HTTP_X_CONTAINER_READ', group + ':' + user]]
    swift_acl['private'] = [['HTTP_X_CONTAINER_WRITE', '.'],
                            ['HTTP_X_CONTAINER_READ', '.']]
    if xml:
        # We are working with XML and need to parse it
        dom = parseString(acl)
        acl = 'unknown'
        for grant in dom.getElementsByTagName('Grant'):
            permission = grant.getElementsByTagName('Permission')[0]\
                .firstChild.data
            grantee = grant.getElementsByTagName('Grantee')[0]\
                .getAttributeNode('xsi:type').nodeValue
            if permission == "FULL_CONTROL" and grantee == 'CanonicalUser' and\
                    acl != 'public-read' and acl != 'public-read-write':
                acl = 'private'
            elif permission == "READ" and grantee == 'Group' and\
                    acl != 'public-read-write':
                acl = 'public-read'
            elif permission == "WRITE" and grantee == 'Group':
                acl = 'public-read-write'
            else:
                acl = 'unsupported'

    if acl == 'authenticated-read':
        return "Unsupported"
    elif acl not in swift_acl:
        return "InvalidArgument"

    return swift_acl[acl]


def validate_bucket_name(name):
    """
    Validates the name of the bucket against S3 criteria,
    http://docs.amazonwebservices.com/AmazonS3/latest/BucketRestrictions.html
    True if valid, False otherwise
    """

    if '_' in name or len(name) < 3 or len(name) > 63 or \
       not name[-1].isalnum():
        # Bucket names should not contain underscores (_)
        # Bucket names must end with a lowercase letter or number
        # Bucket names should be between 3 and 63 characters long
        return False
    elif '.-' in name or '-.' in name or '..' in name or not name[0].isalnum():
        # Bucket names cannot contain dashes next to periods
        # Bucket names cannot contain two adjacent periods
        # Bucket names Must start with a lowercase letter or a number
        return False
    elif re.match("^(([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.){3}"
                  "([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])$", name):
        # Bucket names cannot be formatted as an IP Address
        return False
    else:
        return True


class ServiceController(WSGIContext):
    """
    Handles account level requests.
    """
    def __init__(self, env, app, account_name, token, **kwargs):
        WSGIContext.__init__(self, app)
        env['HTTP_X_AUTH_TOKEN'] = token
        env['PATH_INFO'] = '/v1/%s' % account_name

    def GET(self, env, start_response):
        """
        Handle GET Service request
        """
        env['QUERY_STRING'] = 'format=json'
        body_iter = self._app_call(env)
        status = self._get_status_int()

        if status != HTTP_OK:
            if status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
                return get_err_response('AccessDenied')
            else:
                return get_err_response('InvalidURI')

        containers = loads(''.join(list(body_iter)))
        # we don't keep the creation time of a backet (s3cmd doesn't
        # work without that) so we use something bogus.
        if containers:
            owner = containers[0].get('owner', '')
        else:
            owner = ''
        body = '<?xml version="1.0" encoding="UTF-8"?>' \
               '<ListAllMyBucketsResult ' \
               'xmlns="http://doc.s3.amazonaws.com/2006-03-01">'\
               '<Owner><ID>%s</ID><DisplayName>%s</DisplayName></Owner>'\
               '<Buckets>%s</Buckets>' \
               '</ListAllMyBucketsResult>' \
               % (xml_escape(owner), xml_escape(owner),
                  "".join(['<Bucket><Name>%s</Name><CreationDate>'
                           '2009-02-03T16:45:09.000Z</CreationDate></Bucket>'
                           % xml_escape(i['name']) for i in containers]))
        resp = Response(status=HTTP_OK, content_type='application/xml',
                        body=body)
        return resp


class BucketController(WSGIContext):
    """
    Handles bucket request.
    """
    def __init__(self, env, app, account_name, token, container_name,
                 **kwargs):
        WSGIContext.__init__(self, app)
        self.container_name = unquote(container_name)
        self.account_name = unquote(account_name)
        env['HTTP_X_AUTH_TOKEN'] = token
        env['PATH_INFO'] = '/v1/%s/%s' % (account_name, container_name)
        conf = kwargs.get('conf', {})
        self.location = conf.get('location', 'US')

    def GET(self, env, start_response):
        """
        Handle GET Bucket (List Objects) request
        """
        if 'QUERY_STRING' in env:
            args = dict(urlparse.parse_qsl(env['QUERY_STRING'], 1))
        else:
            args = {}

        if 'max-keys' in args:
            if args.get('max-keys').isdigit() is False:
                return get_err_response('InvalidArgument')

        max_keys = min(int(args.get('max-keys', MAX_BUCKET_LISTING)),
                       MAX_BUCKET_LISTING)

        if 'acl' not in args:
            # acl request sent with format=json etc confuses swift
            env['QUERY_STRING'] = 'format=json&limit=%s' % (max_keys + 1)
        else:
            env['REQUEST_METHOD'] = 'HEAD'
        if 'versions' in args:
            env['QUERY_STRING'] += '&versions'
        if 'marker' in args:
            env['QUERY_STRING'] += '&marker=%s' % quote(args['marker'])
        if 'prefix' in args:
            env['QUERY_STRING'] += '&prefix=%s' % quote(args['prefix'])
        if 'delimiter' in args:
            env['QUERY_STRING'] += '&delimiter=%s' % quote(args['delimiter'])

        body_iter = self._app_call(env)
        status = self._get_status_int()
        headers = dict(self._response_headers)

        if 'acl' in args:
            return get_s3_acl(headers, container_server.ACL_HEADERS,
                              'container')

        if status != HTTP_OK:
            if status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
                return get_err_response('AccessDenied')
            elif status == HTTP_NOT_FOUND:
                return get_err_response('NoSuchBucket')
            else:
                return get_err_response('InvalidURI')

        if 'location' in args:
            body = ('<?xml version="1.0" encoding="UTF-8"?>'
                    '<LocationConstraint '
                    'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"')
            if self.location == 'US':
                body += '/>'
            else:
                body += ('>%s</LocationConstraint>' % self.location)
            return Response(body=body, content_type='application/xml')

        if 'versioning' in args:
            vers = self._response_header_value('x-container-versioning') or ''
            body = (
                '<VersioningConfiguration '
                        'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                '<Status>%s</Status></VersioningConfiguration>' %
                vers.capitalize())
            return Response(body=body, content_type='application/xml')

        if 'logging' in args:
            # logging disabled
            body = ('<?xml version="1.0" encoding="UTF-8"?>'
                    '<BucketLoggingStatus '
                    'xmlns="http://doc.s3.amazonaws.com/2006-03-01" />')
            return Response(body=body, content_type='application/xml')

        objects = loads(''.join(list(body_iter)))
        if 'versions' in args:
            obj_list = []
            for obj in objects:
                if 'subdir' not in obj:
                    if obj['deleted']:
                        name = xml_escape(unquote(obj['name'].encode('utf-8')))
                        obj_list.append(
                            '<DeleteMarker>'
                                '<Key>%s</Key>'
                                '<VersionId>%s</VersionId>'
                                '<IsLatest>%s</IsLatest>'
                                '<LastModified>%s</LastModified>'
                            '</DeleteMarker>' % (
                                name, obj['version_id'],
                                'true' if obj['is_latest'] else 'false',
                                obj['last_modified']
                        ))
                    else:
                        name = xml_escape(unquote(obj['name'].encode('utf-8')))
                        obj_list.append(
                            '<Version>'
                                '<Key>%s</Key>'
                                '<VersionId>%s</VersionId>'
                                '<IsLatest>%s</IsLatest>'
                                '<LastModified>%s</LastModified>'
                                '<ETag>&quot;%s&quot;</ETag>'
                                '<Size>%s</Size>'
                                '<StorageClass>STANDARD</StorageClass>'
                                '<Owner>'
                                    '<ID>%s</ID>'
                                    '<DisplayName>%s</DisplayName>'
                                '</Owner>'
                            '</Version>' % (
                                name, obj['version_id'],
                                'true' if obj['is_latest'] else 'false',
                                obj['last_modified'], obj['hash'],
                                obj['bytes'], obj['owner'], obj['owner']
                        ))
            body = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<ListVersionsResult '
                        'xmlns="http://s3.amazonaws.com/doc/2006-03-01">'
                    '<Prefix>%s</Prefix>'
                    '<KeyMarker>%s</KeyMarker>'
                    '<VersionIdMarker>%s</VersionIdMarker>'
                    '<Delimiter>%s</Delimiter>'
                    '<IsTruncated>%s</IsTruncated>'
                    '<MaxKeys>%s</MaxKeys>'
                    '<Name>%s</Name>'
                    '%s'
                    '%s'
                '</ListVersionsResult>' % (
                xml_escape(args.get('prefix', '')),
                xml_escape(args.get('key-marker', '')),
                xml_escape(args.get('version-id-marker', '')),
                xml_escape(args.get('delimiter', '')),
                'true' if len(objects) == (max_keys + 1) else 'false',
                max_keys,
                xml_escape(self.container_name),
                "".join(obj_list),
                "".join(['<CommonPrefixes><Prefix>%s</Prefix></CommonPrefixes>'
                         % xml_escape(i['subdir'])
                         for i in objects[:max_keys] if 'subdir' in i])))
        else:
            obj_list = []
            prefixes = []
            for i in objects:
                if 'subdir' in i:
                    name = xml_escape(unquote(i['subdir'].encode('utf-8')))
                    prefixes.append('<CommonPrefixes>'
                                    '<Prefix>%s</Prefix>'
                                    '</CommonPrefixes>' % name)
                else:
                    name = xml_escape(unquote(i['name'].encode('utf-8')))
                    owner = i.get('owner', self.account_name)
                    obj_list.append(
                        '<Contents>'
                            '<Key>%s</Key>'
                            '<LastModified>%sZ</LastModified>'
                            '<ETag>%s</ETag>'
                            '<Size>%s</Size>'
                            '<StorageClass>STANDARD</StorageClass>'
                            '<Owner>'
                                '<ID>%s</ID>'
                                '<DisplayName>%s</DisplayName>'
                            '</Owner>'
                        '</Contents>' %
                        (name, i['last_modified'], i['hash'], i['bytes'],
                         owner, owner))
            body = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<ListBucketResult '
                    'xmlns="http://s3.amazonaws.com/doc/2006-03-01">'
                    '<Prefix>%s</Prefix>'
                    '<Marker>%s</Marker>'
                    '<Delimiter>%s</Delimiter>'
                    '<IsTruncated>%s</IsTruncated>'
                    '<MaxKeys>%s</MaxKeys>'
                    '<Name>%s</Name>'
                    '%s'
                    '%s'
                '</ListBucketResult>' % (
                xml_escape(args.get('prefix', '')),
                xml_escape(args.get('marker', '')),
                xml_escape(args.get('delimiter', '')),
                'true' if max_keys > 0 and
                          len(objects) == (max_keys + 1) else 'false',
                max_keys,
                xml_escape(self.container_name),
                ''.join(obj_list),
                ''.join(prefixes)))
        return Response(body=body, content_type='application/xml')

    def PUT(self, env, start_response):
        """
        Handle PUT Bucket request
        """
        if 'CONTENT_LENGTH' in env:
            try:
                content_length = int(env['CONTENT_LENGTH'])
            except (ValueError, TypeError):
                return get_err_response('InvalidArgument')
            if content_length < 0:
                return get_err_response('InvalidArgument')

        if 'QUERY_STRING' in env:
            args = dict(urlparse.parse_qsl(env['QUERY_STRING'], 1))
        else:
            args = {}

        acl = 'acl' in args
        if acl:
            res = acp_to_headers(env, 'container')
            if res:
                return res
            env['REQUEST_METHOD'] = 'POST'

        versioning = 'versioning' in args
        if versioning:
            if 'wsgi.input' not in env:
                return get_err_response(
                    'IllegalVersioningConfigurationException')
            versioning_conf = env['wsgi.input'].read()
            if 'Enabled' in versioning_conf:
                env['HTTP_X_CONTAINER_VERSIONING'] = 'enabled'
            elif 'Suspended' in versioning_conf:
                env['HTTP_X_CONTAINER_VERSIONING'] = 'suspended'
            else:
                return get_err_response(
                    'IllegalVersioningConfigurationException')
            env['REQUEST_METHOD'] = 'POST'

        if not acl and not versioning:
            # Translate the Amazon ACL to something that can be
            # implemented in Swift, 501 otherwise. Swift uses POST
            # for ACLs, whereas S3 uses PUT.
            if 'HTTP_X_AMZ_ACL' in env:
                amz_acl = env['HTTP_X_AMZ_ACL']
                del env['HTTP_X_AMZ_ACL']
                translated_acl = swift_acl_translate(amz_acl)
                if translated_acl == 'Unsupported':
                    return get_err_response('Unsupported')
                elif translated_acl == 'InvalidArgument':
                    return get_err_response('InvalidArgument')
                for header, acl in translated_acl:
                    env[header] = acl

        body_iter = self._app_call(env)
        status = self._get_status_int()

        if status != HTTP_CREATED and status != HTTP_NO_CONTENT:
            if status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
                return get_err_response('AccessDenied')
            elif status == HTTP_ACCEPTED:
                return get_err_response('BucketAlreadyExists')
            else:
                return get_err_response('InvalidURI')

        resp = Response()
        if not versioning:
            resp.headers['Location'] = self.container_name
        resp.status = HTTP_OK
        return resp

    def DELETE(self, env, start_response):
        """
        Handle DELETE Bucket request
        """
        body_iter = self._app_call(env)
        status = self._get_status_int()

        if status != HTTP_NO_CONTENT:
            if status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
                return get_err_response('AccessDenied')
            elif status == HTTP_NOT_FOUND:
                return get_err_response('NoSuchBucket')
            elif status == HTTP_CONFLICT:
                return get_err_response('BucketNotEmpty')
            else:
                return get_err_response('InvalidURI')

        resp = Response()
        resp.status = HTTP_NO_CONTENT
        return resp

    def POST(self, env, start_response):
        """
        Handle POST Bucket request
        """

        return get_err_response('Unsupported')


class ObjectController(WSGIContext):
    """
    Handles requests on objects
    """
    def __init__(self, env, app, account_name, token, container_name,
                 object_name, **kwargs):
        WSGIContext.__init__(self, app)
        self.account_name = unquote(account_name)
        self.container_name = unquote(container_name)
        env['HTTP_X_AUTH_TOKEN'] = token
        env['PATH_INFO'] = '/v1/%s/%s/%s' % (account_name, container_name,
                                             object_name)

    def GETorHEAD(self, env, start_response):
        if env['REQUEST_METHOD'] == 'HEAD':
            head = True
            env['REQUEST_METHOD'] = 'GET'
        else:
            head = False
        if 'QUERY_STRING' in env:
            args = dict(urlparse.parse_qsl(env['QUERY_STRING'], 1))
        else:
            args = {}

        env['QUERY_STRING'] = ''
        if 'acl' in args:
            env['QUERY_STRING'] += 'acl'
            env['REQUEST_METHOD'] = 'HEAD'
        if 'versionId' in args:
            env['QUERY_STRING'] += 'versionId=%s' % args['versionId']

        app_iter = self._app_call(env)

        if head:
            app_iter = None

        status = self._get_status_int()
        headers = dict(self._response_headers)

        if is_success(status):
            if 'QUERY_STRING' in env:
                args = dict(urlparse.parse_qsl(env['QUERY_STRING'], 1))
            else:
                args = {}
            if 'acl' in args:
                resp = get_s3_acl(headers, obj_server.ACL_HEADERS, 'object')
                return resp

            new_hdrs = {}
            for key, val in headers.iteritems():
                _key = key.lower()
                if _key.startswith('x-object-meta-'):
                    new_hdrs['x-amz-meta-' + key[14:]] = val
                elif _key in ('content-length', 'content-type',
                              'content-range', 'content-encoding',
                              'etag', 'last-modified'):
                    new_hdrs[key] = val
            return Response(status=status, headers=new_hdrs, app_iter=app_iter)
        elif status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
            return get_err_response('AccessDenied')
        elif status == HTTP_NOT_FOUND:
            return get_err_response('NoSuchKey')
        else:
            return get_err_response('InvalidURI')

    def HEAD(self, env, start_response):
        """
        Handle HEAD Object request
        """
        return self.GETorHEAD(env, start_response)

    def GET(self, env, start_response):
        """
        Handle GET Object request
        """
        return self.GETorHEAD(env, start_response)

    def PUT(self, env, start_response):
        """
        Handle PUT Object and PUT Object (Copy) request
        """
        if 'QUERY_STRING' in env:
            args = dict(urlparse.parse_qsl(env['QUERY_STRING'], True))
        else:
            args = {}

        acl = 'acl' in args
        if acl:
            res = acp_to_headers(env, 'object')
            if res:
                return res
            env['QUERY_STRING'] = 'acl'
            env['REQUEST_METHOD'] = 'POST'
        else:
            for key, value in env.items():
                if key.startswith('HTTP_X_AMZ_META_'):
                    del env[key]
                    env['HTTP_X_OBJECT_META_' + key[16:]] = value
                elif key == 'HTTP_CONTENT_MD5':
                    if value == '':
                        return get_err_response('InvalidDigest')
                    try:
                        env['HTTP_ETAG'] = value.decode('base64').encode('hex')
                    except:
                        return get_err_response('InvalidDigest')
                    if env['HTTP_ETAG'] == '':
                        return get_err_response('SignatureDoesNotMatch')
                elif key == 'HTTP_X_AMZ_COPY_SOURCE':
                    env['HTTP_X_COPY_FROM'] = value

        body_iter = self._app_call(env)
        status = self._get_status_int()

        success_status = HTTP_ACCEPTED if acl else HTTP_CREATED

        if status != success_status:
            if status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
                return get_err_response('AccessDenied')
            elif status == HTTP_NOT_FOUND:
                return get_err_response('NoSuchBucket')
            elif status == HTTP_UNPROCESSABLE_ENTITY:
                return get_err_response('InvalidDigest')
            else:
                return get_err_response('InvalidURI')

        if not acl and 'HTTP_X_COPY_FROM' in env:
            body = '<CopyObjectResult>' \
                   '<ETag>"%s"</ETag>' \
                   '</CopyObjectResult>' % self._response_header_value('etag')
            return Response(status=HTTP_OK, body=body)

        kwargs = {'status': HTTP_OK}
        if not acl:
            kwargs['etag'] = self._response_header_value('etag')

        return Response(**kwargs)

    def DELETE(self, env, start_response):
        """
        Handle DELETE Object request
        """
        body_iter = self._app_call(env)
        status = self._get_status_int()

        if status != HTTP_NO_CONTENT:
            if status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
                return get_err_response('AccessDenied')
            elif status == HTTP_NOT_FOUND:
                return get_err_response('NoSuchKey')
            else:
                return get_err_response('InvalidURI')

        resp = Response()
        resp.status = HTTP_NO_CONTENT
        return resp


class Swift3Middleware(object):
    """Swift3 S3 compatibility midleware"""
    def __init__(self, app, conf, *args, **kwargs):
        self.app = app
        self.conf = conf
        self.logger = get_logger(self.conf, log_route='swift3')
        self.location = conf.get('location', 'US').upper()

    def get_controller(self, path):
        container, obj = split_path(path, 0, 2, True)
        d = dict(container_name=container, object_name=obj)

        if container and obj:
            return ObjectController, d
        elif container:
            return BucketController, d
        return ServiceController, d

    def __call__(self, env, start_response):
        try:
            return self.handle_request(env, start_response)
        except Exception, e:
            self.logger.exception(e)

    def handle_request(self, env, start_response):
        req = Request(env)
        self.logger.debug('Calling Swift3 Middleware')
        self.logger.debug(req.__dict__)

        if 'AWSAccessKeyId' in req.params:
            try:
                req.headers['Date'] = req.params['Expires']
                req.headers['Authorization'] = \
                    'AWS %(AWSAccessKeyId)s:%(Signature)s' % req.params
            except KeyError:
                return get_err_response('InvalidArgument')(env, start_response)

        if 'Authorization' not in req.headers:
            return self.app(env, start_response)

        try:
            keyword, info = req.headers['Authorization'].split(' ')
        except:
            return get_err_response('AccessDenied')(env, start_response)

        if keyword != 'AWS':
            return get_err_response('AccessDenied')(env, start_response)

        try:
            account, signature = info.rsplit(':', 1)
        except:
            return get_err_response('InvalidArgument')(env, start_response)

        try:
            controller, path_parts = self.get_controller(env['PATH_INFO'])
        except ValueError:
            return get_err_response('InvalidURI')(env, start_response)

        if 'Date' in req.headers:
            date = email.utils.parsedate(req.headers['Date'])
            if date is None:
                return get_err_response('AccessDenied')(env, start_response)

            d1 = datetime.datetime(*date[0:6])
            d2 = datetime.datetime.utcnow()
            epoch = datetime.datetime(1970, 1, 1, 0, 0, 0, 0)

            if d1 < epoch:
                return get_err_response('AccessDenied')(env, start_response)

            delta = datetime.timedelta(seconds=60 * 10)
            if d1 - d2 > delta or d2 - d1 > delta:
                return get_err_response('RequestTimeTooSkewed')(env,
                                                                start_response)

        token = base64.urlsafe_b64encode(canonical_string(req))

        controller = controller(env, self.app, account, token, conf=self.conf,
                                **path_parts)

        if hasattr(controller, req.method):
            res = getattr(controller, req.method)(env, start_response)
        else:
            return get_err_response('InvalidURI')(env, start_response)

        return res(env, start_response)


def filter_factory(global_conf, **local_conf):
    """Standard filter factory to use the middleware with paste.deploy"""
    conf = global_conf.copy()
    conf.update(local_conf)

    def swift3_filter(app):
        return Swift3Middleware(app, conf)

    return swift3_filter
