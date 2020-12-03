from gevent import monkey  # noqa: E402  #  pylint: disable=C0411, C0412, C0413

monkey.patch_all()  # noqa: E402  # pylint: disable=C0411, C0413

import re
import signal
from logging.config import dictConfig
import json
import io
import hashlib
import datetime

from elasticapm.contrib.flask import ElasticAPM
from flask import Flask, render_template, make_response, request, Response
from flask.logging import create_logger
from gevent.pywsgi import WSGIServer
import gevent
from IPy import IP
from lxml import etree
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

from apifiles3 import write_file
from apifiles3 import remove_temp_taric_file
from apifiles3 import rename_taric_file
from apifiles3 import save_temp_taric_file
from apifiles3 import stream_taric_file
from apifiles3 import get_taric_index_file
from apifiles3 import get_taric_filepath
from apifiles3 import get_file_list
from apifiles3 import get_file_size
from apifiles3 import read_file
from apifiles3 import file_exists
from apifiles3 import md5
from apifiles3 import modification_date
from config import (
    API_ROOT,
    APIKEYS,
    APIKEYS_UPLOAD,
    WHITELIST,
    WHITELIST_UPLOAD,
    PORT,
    LOGGING,
    NUM_PROXIES,
    REQUIRE_AUTH_FOR_READS,
    SENTRY_DSN,
    ELASTIC_APM_TOKEN,
    ELASTIC_APM_URL,
    ENVIRONMENT,
)


# Use apifile for file system, apifiles3 for AWS S3


dictConfig(LOGGING)

app = Flask(__name__, static_url_path='/static', static_folder='static')
logger = create_logger(app)


# -----------------------
# HTTP HEADERS / API KEYS
# -----------------------
def get_apikey(request):
    apikey = ""
    if request.headers.get("X-API-KEY", None):
        apikey = request.headers.get("X-API-KEY")
        logger.info("Api key is in header")
    else:
        logger.info("No api key in header")

    return apikey


def get_remoteaddr(request):
    if request.environ.get("HTTP_X_FORWARDED_FOR") is None:
        logger.info("Remote addresses are %s", request.environ["REMOTE_ADDR"])
        remoteaddrs = request.environ["REMOTE_ADDR"].split(",")
    else:
        logger.info("Remote addresses are %s", request.environ["HTTP_X_FORWARDED_FOR"])
        remoteaddrs = request.environ["HTTP_X_FORWARDED_FOR"].split(",")

    if len(remoteaddrs) > NUM_PROXIES:
        logger.warning("Additional remote addresses stripped (possible spoofing)")
        remoteaddrs = remoteaddrs[-NUM_PROXIES:]

    return remoteaddrs


def in_whitelist(remoteaddrs):
    for addr in remoteaddrs:
        for wlip in WHITELIST:
            logger.debug("%s %s", addr, wlip)
            if addr in IP(wlip):
                return True
    return False


def in_whitelist_upload(remoteaddrs):
    for addr in remoteaddrs:
        for wlip in WHITELIST_UPLOAD:
            logger.debug("%s %s", addr, wlip)
            if addr in IP(wlip):
                return True
    return False


def in_apikeys(apikey):
    hashed_apikey = str(hashlib.sha256(apikey.encode("ascii")).hexdigest())
    try:
        return hashed_apikey in APIKEYS

    except ValueError:
        return False


def in_apikeys_upload(apikey):
    hashed_apikey = hashlib.sha256(apikey.encode("ascii")).hexdigest()
    try:
        return hashed_apikey in APIKEYS_UPLOAD
    except ValueError:
        return False


def is_auth(request):
    if REQUIRE_AUTH_FOR_READS:
        apikey = get_apikey(request)
        remoteaddr = get_remoteaddr(request)
        return in_apikeys(apikey) and in_whitelist(remoteaddr)

    return True


def is_auth_upload(request):
    apikey = get_apikey(request)
    remoteaddrs = get_remoteaddr(request)

    return in_apikeys_upload(apikey) and in_whitelist_upload(remoteaddrs)


# ---------------------------
# URL Parameter validation
# Dates as ISO8601 YYYY-MM-DD
# Files as YYSSSS
# ---------------------------
def is_valid_date(date):
    return re.match(r"^\d{4}-\d\d-\d\d$", date)


def is_valid_datetime(date):
    return re.match(r"^\d{4}-\d\d-\d\d(T\d\d:\d\d:\d\d(\.\d\d\d)?)?$", date)


def is_valid_seq(seq):
    return re.match(r"^\d{6}$", seq)


def is_virus_checked(file):
    # TODO
    return True


def is_schema_validated(xmlfile):
    logger.debug("VALIDATING %s", xmlfile)

    xsd_doc = etree.parse("taric3.xsd")
    xsd = etree.XMLSchema(xsd_doc)

    try:
        xml = etree.parse(io.BytesIO(read_file(xmlfile)))

    except Exception:  # pylint: disable=W0703
        logger.info("Unable to parse file as XML")
        return False

    if not xsd.validate(xml):
        logger.info("XML Failed validation")
        logger.debug("%s", xsd.error_log)
    else:
        logger.info("XML validates against taric3 schema")

    return xsd.validate(xml)


# ------------------
# Create index entry
# ------------------
def create_index_entry(seq):
    index_entry = {
        "id": int(seq),
        "issue_date": modification_date(get_taric_filepath(seq)),
        "url": API_ROOT + "taricfiles/" + seq,
        "md5": md5(get_taric_filepath(seq)),
        "size": get_file_size(get_taric_filepath(seq)),
    }
    return index_entry


# --------------------------------
# Rebuild master file index (JSON)
# --------------------------------
def rebuild_index(nocheck):
    if not file_exists(get_taric_index_file()) or nocheck:
        logger.info("*** Rebuilding file index... ***")
        all_deltas = []

        files = get_file_list(None)
        logger.info("%s", files)
        for file in files:
            # build entry for file just uploaded
            # TODO (possibly) Add Metadata generation -> then could have api /taricfilemd/...
            # TODO - combine with individual update_index..
            f = file["Key"]
            f = f[f.rindex("/") + 1 :]  # remove folder prefix
            logger.info("Found file %s", f)

            if f.startswith("TEMP_"):
                logger.info("Removing temporary file %s", f)
                seq = f[5:-4]  # remove TEMP_ file prefix and .xml extension
                remove_temp_taric_file(seq)
            else:
                if is_valid_seq(f[:-4]):  # ignore non taric files
                    seq = f[:-4]  # remove .xml extension
                    all_deltas.append(create_index_entry(seq))

        logger.debug("%s delta files listed after update", str(len(all_deltas)))

        # persist updated index
        all_deltass = json.dumps(all_deltas)
        write_file(get_taric_index_file(), all_deltass)


@app.route("/api/v1/rebuildindex", methods=["POST"])
def rebuild_index_controller():
    if not is_auth_upload(request):
        logger.info("API key not provided or not authorised")
        return Response("403 Unauthorised", status=403)

    rebuild_index(True)
    return Response("200 Index rebuilt", status=200)


# -------------------------------
# Update master file index (JSON)
# -------------------------------


def update_index(seq):
    all_deltas = json.loads(read_file(get_taric_index_file()))
    logger.debug(
        "%s delta files listed in %s", str(len(all_deltas)), get_taric_index_file()
    )

    # build entry for file just uploaded
    # TODO (possibly) Add Metadata file generation -> then could have api /taricfilesmd/...

    # if the file was overwritten, just update the index, else append
    existing = [d for d in all_deltas if d["id"] == int(seq)]
    if len(existing) > 0:
        logger.info("File %s overwritten", seq)
        i = 0
        for d in all_deltas:
            logger.debug("%s", d)
            if d["id"] == int(seq):
                all_deltas[i] = create_index_entry(seq)
            i = i + 1
    else:
        all_deltas.append(create_index_entry(seq))

    logger.debug("%s delta files listed after update", str(len(all_deltas)))

    # persist updated index
    all_deltass = json.dumps(all_deltas)
    write_file(get_taric_index_file(), all_deltass)


# ---------------------------------------------
# index page - could be used for pings / checks
# ---------------------------------------------
@app.route("/check")
def check():
    logger.debug("%s", request.headers)
    logger.debug("%s", request.environ)
    message = (
        "Request from "
        + get_apikey(request)
        + " @ "
        + " ".join(get_remoteaddr(request))
    )
    return render_template("check.html", message=message)


@app.route("/healthcheck")
def healthcheck():
    return Response(
        """
<?xml version="1.0" encoding="UTF-8"?>
<pingdom_http_custom_check>
    <status>OK</status>
</pingdom_http_custom_check>
""",
        status=200,
        headers={
            "Content-Type": "text/xml",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@app.route("/")
def hello():
    return render_template("index.html")


# --------------------------------------------------------------------------------------------
# API to retrieve list of delta files (for a date or defaults to yesterday to get latest file)
# NB using today would provide files loaded today
# but no guarantee that the list may change (i.e. extend) later due to further files
# --------------------------------------------------------------------------------------------


@app.route("/api/v1/taricdeltas/<date>", methods=["GET"])
@app.route("/api/v1/taricdeltas/", defaults={"date": ""}, methods=["GET"])
@app.route("/api/v1/taricdeltas", defaults={"date": ""}, methods=["GET"])
def taricdeltas(date):

    # Default to yesterday
    if date == "" or date is None:
        yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
        date = yesterday.strftime("%Y-%m-%d")
        logger.debug("defaulted date to %s", date)

    if not is_valid_date(date):
        logger.debug("date is invalid")
        return Response("Bad request [invalid date] (400)", status=400)

    if not is_auth(request):
        logger.debug("API key not provided or not authorised")
        return Response("403 Unauthorised", status=403)

    logger.debug("date is %s", date)

    # All Taric files uploaded are stored in the index
    # Find files that have the issue date the same as the requested date
    # Output the response filtered by the date
    all_deltas = json.loads(read_file(get_taric_index_file()))
    logger.debug(
        "%s delta files listed in %s", str(len(all_deltas)), get_taric_index_file()
    )

    deltas_on_date = [d for d in all_deltas if d["issue_date"].startswith(date)]

    if len(deltas_on_date) == 0:
        logger.debug("No delta files available for date %s", date)
        return Response("404 Not found", status=404)

    logger.debug("%s delta files for date %s", str(len(deltas_on_date)), date)

    deltas_json = json.dumps(deltas_on_date)

    r = make_response(deltas_json)

    r.headers.set("Content-Type", "application/json")
    return r


# -----------------------------------------
# API to retrieve contents of specific file
# -----------------------------------------
@app.route("/api/v1/taricfiles/<seq>", methods=["GET"])
@app.route("/api/v1/taricfiles", defaults={"seq": ""}, methods=["GET"])
def taricfiles(seq):

    if not is_auth(request):
        logger.debug("API key not provided or not authorised")
        return Response("403 Unauthorised", status=403)

    if not is_valid_seq(seq):
        logger.debug("seq is invalid")
        return Response("400 Bad request [invalid seq]", status=400)

    body_generator = stream_taric_file(seq)
    if body_generator is None:
        logger.debug("Requested file not found %s", seq)
        return Response("404 Taric file does not exist", status=404)

    return Response(body_generator, mimetype="text/xml")


# --------------------------------------------------------------------
# API to upload new taric file
# File in the API is identified by seq regardless of it's source name
# File modification time can be set using ?modtime=yyyy-mm-ddThh:mm:ss
# --------------------------------------------------------------------
@app.route("/api/v1/taricfiles/<seq>", methods=["POST"])
@app.route("/api/v1/taricfiles", defaults={"seq": ""}, methods=["POST"])
def taricfiles_upload(seq):

    modtime = None

    if not is_auth_upload(request):
        logger.debug("API key not provided or not authorised")
        return Response("403 Unauthorised", status=403)

    if not is_valid_seq(seq):
        logger.debug("seq is invalid")
        return Response("400 Bad request [invalid seq]", status=400)

    if "file" not in request.files:
        logger.debug("No file uploaded")
        return Response("400 No file uploaded", status=400)

    # file is that attached in the POST request
    file = request.files["file"]

    if not file or file.filename == "":
        logger.debug("No file uploaded")
        return Response("400 No file uploaded", status=400)

    logger.debug("file uploaded is %s", file.filename)

    if not request.args.get("modtime") is None:
        if not is_valid_datetime(request.args.get("modtime")):
            logger.debug(
                "Invalid file modification timestamp specified %s",
                request.args.get("modtime"),
            )
            return Response(
                "400 Invalid file modification timestamp specified", status=400
            )
        else:
            modtime = request.args.get("modtime")
            logger.debug("file mod time is %s", modtime)

    # Save the uploaded XML file as temporary
    temp_file_name = save_temp_taric_file(file, seq)

    # TODO - should virus check ..
    if not is_virus_checked(file.read()):
        logger.debug("File failed virus check")
        remove_temp_taric_file(seq)
        return Response("400 Failed virus check", status=400)

    # Validate XML against XSD
    if not is_schema_validated(temp_file_name):
        logger.debug("File failed schema check")
        remove_temp_taric_file(seq)
        return Response("400 Failed schema check", status=400)

    # Rename the temporary XML file and update the index - used by the deltas API
    try:
        rename_taric_file(seq, modtime)
        update_index(seq)
    except IOError as exc:
        logger.error("Error saving file %s.xml: %s", seq, str(exc))
        return Response("500 Error saving file", status=500)

    return Response("200 OK File uploaded", status=200)


def get_server():
    if SENTRY_DSN:
        sentry_sdk.init(
            dsn=SENTRY_DSN, integrations=[FlaskIntegration()],
        )

    @app.after_request
    def add_x_robots(response):  # pylint: disable=W0612
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return response

    elastic_apm_url = ELASTIC_APM_URL
    elastic_apm_secret_token = ELASTIC_APM_TOKEN
    elastic_apm = (
        {
            'SERVICE_NAME': 'public-tariffs-api',
            'SECRET_TOKEN': elastic_apm_secret_token,
            'SERVER_URL': elastic_apm_url,
            'ENVIRONMENT': ENVIRONMENT,
        }
        if elastic_apm_url and elastic_apm_secret_token
        else {}
    )

    if elastic_apm:
        app.config['ELASTIC_APM'] = elastic_apm
        ElasticAPM(app)

    server = WSGIServer(("0.0.0.0", PORT), app, log=app.logger)

    return server


def main():
    rebuild_index(False)
    server = get_server()

    gevent.signal_handler(signal.SIGTERM, server.stop)
    gevent.signal_handler(signal.SIGTERM, server.stop)

    server.serve_forever()
    gevent.get_hub().join()


if __name__ == "__main__":
    main()
