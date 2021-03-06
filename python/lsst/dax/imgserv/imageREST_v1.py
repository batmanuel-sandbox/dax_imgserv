# LSST Data Management System
# Copyright 2017 AURA/LSST.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <http://www.lsstcorp.org/LegalNotices/>.

"""
This module implements the RESTful interface for Image Cutout Service.
Corresponding URI: /image

@author: John Gates, SLAC
@author: Brian Van Klaveren, SLAC
@author: Kenny Lo, SLAC

"""
import os
import tempfile
import traceback
import json
from jsonschema import validate, ValidationError

from flask import Blueprint, make_response, request, current_app, jsonify
from flask import render_template, send_file

import lsst.log as log
import lsst.afw.fits as fits
import lsst.afw.image as afwImage

from http.client import BAD_REQUEST, INTERNAL_SERVER_ERROR, NOT_FOUND
from .locateImage import image_open_v1, W13DeepCoaddDb, W13RawDb, W13CalexpDb

from .dispatch_v1 import Dispatcher
from .jsonutil import get_params

imageRESTv1 = Blueprint("imageRESTv1", __name__, static_folder="static",
                        template_folder="templates")


# To be called from webserv
def load_imgserv_config(config_path, metaserv_url):
    """Load configuration info into ImageServ."""
    if config_path is None:
        # use default root_path for imageRESTv1
        config_path = imageRESTv1.root_path+"/config/"
    f_json = os.path.join(config_path, "imgserv_conf.json")
    # load the general config file
    current_app.config.from_json(f_json)
    # configure the log file (log4cxx)
    log.configure(os.path.join(config_path, "log.properties"))
    current_app.config["DAX_IMG_META_URL"] = metaserv_url
    current_app.config["DAX_IMG_CONFIG"] = config_path
    current_app.config["imageREST_v1"]=os.path.join(config_path,
    "imageREST_v1.schema")
    # create cache for butler instances
    current_app.butler_instances = {}

@imageRESTv1.route("/")
def index():
    return make_response(render_template("index_v1.html"))


@imageRESTv1.route("/availability", methods=["GET"])
def getimage_avail():
    return _getimage_avail(request)


@imageRESTv1.route("/capabilities", methods=["GET"])
def getimage_capabilities():
    return _getimage_capabilities(request)


@imageRESTv1.route("/<db_id>", methods=["GET", "PUT", "POST"])
def getimage_sync(db_id):
    return _getimage(request, db_id)


@imageRESTv1.route("/<db_id>/async", methods=["POST"])
def getimage_async(db_id):
    return _getimage_async(request)


@imageRESTv1.errorhandler(Exception)
def handle_unhandled_exceptions(error):
    err = {
        "exception": error.__class__.__name__,
        "message": error.args[0],
        "traceback": traceback.format_exc()
    }
    if len(error.args) > 1:
        err["more"] = [str(arg) for arg in error.args[1:]]
    response = jsonify(err)
    response.status_code = INTERNAL_SERVER_ERROR
    return response


def _getimage_async(_req):
    # ToDo: TBD
    raise NotImplementedError("async endpoint not yet implemented")


def _getimage_avail(_req):
    """Return availability status."""
    fmt = _req.accept_mimetypes.best_match(["application/json", "text/html"])
    if fmt == "text/html":
        resp = "<h1> Image Web Service v1 </h1> <p> \
                Service is accepting queries."
    else:
        resp = json.dumps({
            "status": "ImageServ v1 is accepting queries.",
            "available": 'true'
        })
    return make_response(resp)


def _getimage_capabilities(_req):
    """Return capabilities of this service."""
    fmt = _req.accept_mimetypes.best_match(["application/json", "text/html"])
    if fmt == "text/html":
        resp = "<h1> Image Web Service v1 </h1> <p> \
                <a href='availability'> \
                check availability </a> <p> \
                <a href='/image/v1'> \
                New query </a> <p> \
                <a href='capabilities'> \
                check capabilities </a>"
    else:
        resp = json.dumps({
                "availability": {
                    "url": "availability"
                },
                "query": {
                    "url": "DC_W13_Strip82?",
                    "interface": ["DAX"]
                },
                "capabilities": {
                    "url": "capabilities"
                }
            })
    return make_response(resp)


def _getimage(_req, db_id):
    """Get the image per query request synchronously (default).
    Parameters:
        request the request object
        db  image database string
    """
    if _req.is_json:
        r_data =  _req.get_json()
        # schema validation check
        check = current_app.config["DAX_IMG_VALIDATE"]
        if check:
            f_schema = current_app.config["imageREST_v1"]
            with open(f_schema) as f:
                schema = json.load(f)
                f.close()
            validate(r_data, schema)
        params = get_params(r_data)
        ds = r_data["image"]["ds"]
    else:
        if _req.content_type and "form" in _req.content_type:
            # e.g. Content-Type: application/www-form-urlencoded
            params = _req.form.copy()
        else:
            # GET
            params = _req.args.copy()
    current_app.config["DAX_IMG_META_DB"] = db_id
    params["db"] = db_id
    ds = params["ds"]
    if ds is None:
        return _db_not_found("Mising ds parameter")
    dispatcher = Dispatcher(current_app.config["DAX_IMG_CONFIG"])
    api = dispatcher.find_api(params)
    if api is None:
        raise Exception("Dispatcher failed to find matching API")
    w13db = _get_ds(ds.strip())
    if w13db is None:
        return _db_not_found()
    img_getter = image_open_v1(w13db, current_app.config)
    if img_getter is None:
        raise Exception("Failed to instantiate ImageGetter")
    image = api(img_getter, params)
    if image:
        return _data_response(image)
    else:
        return _image_not_found()


def _get_ds(image_type):
    # use lower case
    it = image_type.lower()
    if it == "raw":
        return W13RawDb
    elif it == "calexp":
        return W13CalexpDb
    elif it == "deepcoadd":
        return W13DeepCoaddDb


def _data_response(image):
    """Write image data to FITS file and send back.

    Parameters
    ----------
    image: lsst.afw.image.Exposure

    Returns
    -------
    flask.send_file
        the image as FITS file attachement.
    """
    if isinstance(image, (afwImage.Exposure, afwImage.Image)):
        fp = tempfile.NamedTemporaryFile()
        image.writeFits(fp.name)
        res = send_file(fp.name,
                mimetype="image/fits",
                as_attachment=True,
                attachment_filename="image.fits")
    else:
        res = jsonify(image)
    return res


def _image_not_found(message=None):
    # HTTP 404 - NOT FOUND, RFC2616, Section 10.4.5
    if not message:
        message = "Image Not Found"
    response = jsonify({"exception": IOError.__name__, "message": message})
    response.status_code = NOT_FOUND  # ValueError == BAD REQUEST
    return response


def _db_not_found(message=None):
    # HTTP 404 - NOT FOUND, RFC2616, Section 10.4.5
    if not message:
        message = "Db Not Found"
    response = jsonify({"exception": IOError.__name__, "message": message})
    response.status_code = NOT_FOUND
    return response


