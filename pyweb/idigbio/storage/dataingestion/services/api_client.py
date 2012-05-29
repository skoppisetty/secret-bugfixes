#!/usr/bin/env python
#
# Copyright (c) 2012 Xu, Jiang Yan <me@jxu.me>, University of Florida
#
# This software may be used and distributed according to the terms of the
# MIT license: http://www.opensource.org/licenses/mit-license.php
"""
This module encapulates the communication with iDigBio's storage service API.
"""
import argparse, json, urllib2, logging
import uuid
from poster.encode import multipart_encode
from poster.streaminghttp import register_openers

import socket
try:
    from eventlet import sleep
except ImportError:
    from time import sleep

try:
    from eventlet.green.httplib import HTTPException, HTTPConnection
except ImportError:
    from httplib import HTTPException, HTTPConnection


BASE_URL = 'http://dev.idigbio.org:9191/v1'

logger = logging.getLogger("iDigBioSvc.api_client")

register_openers()

def build_url(collection, entity_uuid=None, subcollection=None):
    if entity_uuid is None:
        ret = "%s/%s/" % (BASE_URL, collection)
    elif subcollection is None:
        ret = "%s/%s/%s/" % (BASE_URL, collection, entity_uuid)
    else:
        ret = "%s/%s/%s/%s/" % (BASE_URL, collection, entity_uuid, subcollection)
    
    logger.debug("URL built: %s" % ret)
    return ret


def _post_recordset():
    providerid = str(uuid.uuid4())
    data = { "idigbio:data": { "ac:variant": "IngestionTool" },
            "idigbio:providerId": providerid }
    url = build_url("recordsets")
    try:
        response = _post_json(url, data)
    except urllib2.HTTPError as e:
        raise ClientException("Failed to POST the recordset to server.",
                              url=url, http_status=e.code, 
                              http_response_content=e.read(),
                              reason=providerid)
    logger.debug("Response: {0}".format(response))
    return response['idigbio:uuid']

def _post_mediarecord(recordset_uuid):
    data = { "idigbio:data": { "ac:variant": "IngestionTool" },
            "idigbio:providerId": str(uuid.uuid4()),
            "idigbio:parentUuid": recordset_uuid }
    url = build_url("mediarecords")
    try:
        response = _post_json(url, data)
    except urllib2.HTTPError as e:
        raise ClientException("Failed to POST the mediarecord to server.",
                              url=url, http_status=e.code, 
                              http_response_content=e.read(),
                              reason=recordset_uuid)
    logger.debug("Response: {0}".format(response))
    return response['idigbio:uuid']

def _post_media(local_path, entity_uuid):
    url = build_url("mediarecords", entity_uuid, "media")
    datagen, headers = multipart_encode({"file": open(local_path, "rb")})
    try:
        request = urllib2.Request(url, datagen, headers)
        resp = urllib2.urlopen(request).read()
        logger.debug("Response: {0}".format(resp))
        return json.loads(resp)
    except urllib2.HTTPError as e:
        raise ClientException("Failed to POST the JSON to server.",
                              url=request.get_full_url(), http_status=e.code,
                              http_response_content=e.read(),
                              reason=entity_uuid, local_path=local_path)


def _post_json(url, obj):
    """
    :returns: the reponse JSON object.
    """
    content = json.dumps(obj, separators=(',',':'))
    req = urllib2.Request(url, content, {'Content-Type': 'application/json'})
    r = urllib2.urlopen(req)
    resp = r.read()
    json_response = json.loads(resp)
    return json_response

def upload_image_primitive(path):
    try:
        set_uuid = _post_recordset()
        mediarecord_uuid = _post_mediarecord(set_uuid)
        resp_json = _post_media(path, mediarecord_uuid)
        img_url = resp_json['idigbio:links']['media']
        return img_url
    except ClientException as e:
        logger.error("Upload failed: {0}.".format(e))
        raise
    
def upload_image_with_retries(path):
    conn = Connection()
    try:
        set_uuid = conn.post_recordset()
        mediarecord_uuid = conn.post_mediarecord(set_uuid)
        resp_json = conn.post_media(path, mediarecord_uuid)
        img_url = resp_json['idigbio:links']['media']
        return img_url
    except ClientException as err:
        logger.error("Upload failed after retries: {0}.".format(err))
        

class ClientException(Exception):
    def __init__(self, msg, url='', http_status=0, reason='', local_path='',
                 http_response_content=''):
        Exception.__init__(self, msg)
        self.msg = msg
        self.url = url
        self.http_status = http_status
        self.reason = reason
        self.local_path = local_path
        self.http_response_content = http_response_content

    def __str__(self):
        a = self.msg
        b = ''
        if self.url:
            b += self.url
        if self.http_status:
            if b:
                b = '%s %s' % (b, self.http_status)
            else:
                b = str(self.http_status)
        if self.reason:
            if b:
                b = '%s %s' % (b, self.reason)
            else:
                b = '- %s' % self.reason
        if self.local_path:
            if b:
                b = '%s %s' % (b, self.local_path)
            else:
                b = '- %s' % self.local_path
        if self.http_response_content:
            if len(self.http_response_content) <= 200:
                b += '   %s' % self.http_response_content
            else:
                b += '  [first 60 chars of response] %s' % \
                   self.http_response_content[:200]
        return b and '%s: %s' % (a, b) or a

class Connection(object):
    """Convenience class to make requests that will also retry the request"""

    def __init__(self, authurl=None, user=None, key=None, retries=5, preauthurl=None,
                 preauthtoken=None, snet=False, starting_backoff=1,
                 auth_version="1"):
        """
        :param authurl: authenitcation URL
        :param user: user name to authenticate as
        :param key: key/password to authenticate with
        :param retries: Number of times to retry the request before failing
        :param preauthurl: storage URL (if you have already authenticated)
        :param preauthtoken: authentication token (if you have already
                             authenticated)
        :param snet: use SERVICENET internal network default is False
        :param auth_version: Openstack auth version.
        """
        self.authurl = authurl
        self.user = user
        self.key = key
        self.retries = retries
        self.http_conn = None
        self.url = preauthurl
        self.token = preauthtoken
        self.attempts = 0
        self.snet = snet
        self.starting_backoff = starting_backoff
        self.auth_version = auth_version

    def _retry(self, reset_func, func, *args, **kwargs):
        self.attempts = 0
        backoff = self.starting_backoff
        while self.attempts <= self.retries:
            self.attempts += 1
            try:
                rv = func(*args, **kwargs)
                return rv
            except ClientException, err:
                logger.debug("Exception caught: {0}".format(err))
                logger.debug("Current retry attempts: {0}".format(self.attempts))
                logger.debug("Current backoff: {0}".format(backoff))
                
                if self.attempts > self.retries:
                    raise
                if err.http_status == 401: # Unauthorized
                    if self.attempts > 1:
                        raise
                elif err.http_status == 408: # Request Timeout
                    pass
                elif 500 <= err.http_status <= 599:
                    pass
                else:
                    raise
            except urllib2.URLError as err:
                logger.debug("Exception caught: {0}".format(err.reason))
                logger.debug("Current retry attempts: {0}".format(self.attempts))
                logger.debug("Current backoff: {0}".format(backoff))
                if self.attempts > self.retries:
                    raise
            
            sleep(backoff)
            backoff *= 2
            if reset_func:
                reset_func(func, *args, **kwargs)
                
    def post_recordset(self):
        return self._retry(None, _post_recordset)
    
    def post_mediarecord(self, recordset_uuid):
        return self._retry(None, _post_mediarecord, recordset_uuid)
    
    def post_media(self, local_path, entity_uuid):
        return self._retry(None, _post_media, local_path, entity_uuid)

def main():
    logging.basicConfig(level=logging.DEBUG)
    parser = argparse.ArgumentParser()
    parser.add_argument("image_path")
    parser.add_argument("-r", "--retry", action='store_true')
    parser.add_argument("-b", "--browser", action='store_true')
    args = parser.parse_args()
    
    if args.retry:
        img_url = upload_image_with_retries(args.image_path)
    else:
        img_url = upload_image_primitive(args.image_path)
    logger.info("URL for the uploaded image: {0}".format(img_url))
    
    if args.browser:
        import webbrowser
        webbrowser.open(img_url)
    
if __name__ == '__main__':
    main()