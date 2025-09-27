import logging
import os
import re
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from deluge_web_client import DelugeWebClient as delugewebclient
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from qbittorrentapi import Client
from transmission_rpc import Client as transmissionrpc

app = Flask(__name__)


# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "logs/app.log")

log_dir = os.path.dirname(LOG_FILE)
if log_dir:
    os.makedirs(log_dir, exist_ok=True)

log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5)
file_handler.setFormatter(log_formatter)

logging.basicConfig(level=LOG_LEVEL, handlers=[stream_handler, file_handler])

logger = logging.getLogger(__name__)

#Load environment variables
load_dotenv()

ABB_HOSTNAME = os.getenv("ABB_HOSTNAME", "audiobookbay.lu")

PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", 5))

DOWNLOAD_CLIENT = os.getenv("DOWNLOAD_CLIENT")
DL_URL = os.getenv("DL_URL")
if DL_URL:
    parsed_url = urlparse(DL_URL)
    DL_SCHEME = parsed_url.scheme
    DL_HOST = parsed_url.hostname
    DL_PORT = parsed_url.port
else:
    DL_SCHEME = os.getenv("DL_SCHEME", "http")
    DL_HOST = os.getenv("DL_HOST")
    DL_PORT = os.getenv("DL_PORT")

    # Make a DL_URL for Deluge if one was not specified
    if DL_HOST and DL_PORT:
        DL_URL = f"{DL_SCHEME}://{DL_HOST}:{DL_PORT}"

DL_USERNAME = os.getenv("DL_USERNAME")
DL_PASSWORD = os.getenv("DL_PASSWORD")
DL_CATEGORY = os.getenv("DL_CATEGORY", "Audiobookbay-Audiobooks")
SAVE_PATH_BASE = os.getenv("SAVE_PATH_BASE")

# Custom Nav Link Variables
NAV_LINK_NAME = os.getenv("NAV_LINK_NAME")
NAV_LINK_URL = os.getenv("NAV_LINK_URL")

# Print configuration
logger.info("ABB_HOSTNAME: %s", ABB_HOSTNAME)
logger.info("DOWNLOAD_CLIENT: %s", DOWNLOAD_CLIENT)
logger.info("DL_HOST: %s", DL_HOST)
logger.info("DL_PORT: %s", DL_PORT)
logger.info("DL_URL: %s", DL_URL)
logger.info("DL_USERNAME: %s", DL_USERNAME)
logger.info("DL_CATEGORY: %s", DL_CATEGORY)
logger.info("SAVE_PATH_BASE: %s", SAVE_PATH_BASE)
logger.info("NAV_LINK_NAME: %s", NAV_LINK_NAME)
logger.info("NAV_LINK_URL: %s", NAV_LINK_URL)
logger.info("PAGE_LIMIT: %s", PAGE_LIMIT)


@app.context_processor
def inject_nav_link():
    return {
        'nav_link_name': os.getenv('NAV_LINK_NAME'),
        'nav_link_url': os.getenv('NAV_LINK_URL')
    }



# Helper function to search AudiobookBay
def search_audiobookbay(query, max_pages=PAGE_LIMIT):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0'
    }
    results = []
    for page in range(1, max_pages + 1):
        url = f"https://{ABB_HOSTNAME}/page/{page}/?s={query.replace(' ', '+')}&cat=undefined%2Cundefined"
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            logger.error(
                "Failed to fetch page %s. Status Code: %s", page, response.status_code
            )
            break

        soup = BeautifulSoup(response.text, 'html.parser')
        for post in soup.select('.post'):
            try:
                title = post.select_one('.postTitle > h2 > a').text.strip()
                link = f"https://{ABB_HOSTNAME}{post.select_one('.postTitle > h2 > a')['href']}"
                cover = post.select_one('img')['src'] if post.select_one('img') else "/static/images/default-cover.jpg"
                results.append({'title': title, 'link': link, 'cover': cover})
            except Exception as e:
                logger.exception("Skipping post due to error: %s", e)
                continue
    return results

# Helper function to extract magnet link from details page
def extract_magnet_link(details_url):
    headers = {
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0'
    }
    try:
        response = requests.get(details_url, headers=headers)
        if response.status_code != 200:
            logger.error(
                "Failed to fetch details page. Status Code: %s", response.status_code
            )
            return None

        soup = BeautifulSoup(response.text, 'html.parser')

        # Extract Info Hash
        info_hash_row = soup.find('td', string=re.compile(r'Info Hash', re.IGNORECASE))
        if not info_hash_row:
            logger.error("Info Hash not found on the page.")
            return None
        info_hash = info_hash_row.find_next_sibling('td').text.strip()

        # Extract Trackers
        tracker_rows = soup.find_all('td', string=re.compile(r'udp://|http://', re.IGNORECASE))
        trackers = [row.text.strip() for row in tracker_rows]

        if not trackers:
            logger.warning("No trackers found on the page. Using default trackers.")
            trackers = [
                "udp://tracker.openbittorrent.com:80",
                "udp://opentor.org:2710",
                "udp://tracker.ccc.de:80",
                "udp://tracker.blackunicorn.xyz:6969",
                "udp://tracker.coppersurfer.tk:6969",
                "udp://tracker.leechers-paradise.org:6969"
            ]

        # Construct the magnet link
        trackers_query = "&".join(f"tr={requests.utils.quote(tracker)}" for tracker in trackers)
        magnet_link = f"magnet:?xt=urn:btih:{info_hash}&{trackers_query}"

        logger.debug("Generated Magnet Link: %s", magnet_link)
        return magnet_link

    except Exception as e:
        logger.exception("Failed to extract magnet link: %s", e)
        return None

# Helper function to sanitize titles
def sanitize_title(title):
    return re.sub(r'[<>:"/\\|?*]', '', title).strip()

# Endpoint for search page
@app.route('/', methods=['GET', 'POST'])
def search():
    books = []
    try:
        if request.method == 'POST':  # Form submitted
            query = request.form['query']
            #Convert to all lowercase
            query = query.lower()
            if query:  # Only search if the query is not empty
                books = search_audiobookbay(query)
        return render_template('search.html', books=books)
    except Exception as e:
        logger.exception("Failed to search: %s", e)
        return render_template('search.html', books=books, error=f"Failed to search. { str(e) }")




# Endpoint to send magnet link to qBittorrent
@app.route('/send', methods=['POST'])
def send():
    data = request.json
    details_url = data.get('link')
    title = data.get('title')
    if not details_url or not title:
        return jsonify({'message': 'Invalid request'}), 400

    try:
        magnet_link = extract_magnet_link(details_url)
        if not magnet_link:
            return jsonify({'message': 'Failed to extract magnet link'}), 500

        save_path = f"{SAVE_PATH_BASE}/{sanitize_title(title)}"
        
        if DOWNLOAD_CLIENT == 'qbittorrent':
            qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            qb.auth_log_in()
            qb.torrents_add(urls=magnet_link, save_path=save_path, category=DL_CATEGORY)
        elif DOWNLOAD_CLIENT == 'transmission':
            transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, protocol=DL_SCHEME, username=DL_USERNAME, password=DL_PASSWORD)
            transmission.add_torrent(magnet_link, download_dir=save_path)
        elif DOWNLOAD_CLIENT == "delugeweb":
            delugeweb = delugewebclient(url=DL_URL, password=DL_PASSWORD)
            delugeweb.login()
            try:
                delugeweb.add_torrent_magnet(
                    magnet_link, save_directory=save_path, label=DL_CATEGORY
                )
            except Exception as deluge_error:
                error_message = str(deluge_error)
                logger.warning(
                    "Deluge raised an error when applying label: %s", error_message
                )
                if "Unknown method" in error_message and "label" in error_message:
                    logger.info("Retrying Deluge upload without applying a label.")
                    delugeweb.add_torrent_magnet(
                        magnet_link, save_directory=save_path, label=None
                    )
                else:
                    raise
        else:
            return jsonify({'message': 'Unsupported download client'}), 400

        return jsonify({'message': f'Download added successfully! This may take some time, the download will show in Audiobookshelf when completed.'})
    except Exception as e:
        logger.exception("Failed to send magnet link: %s", e)
        return jsonify({'message': str(e)}), 500
@app.route('/status')
def status():
    try:
        if DOWNLOAD_CLIENT == 'transmission':
            transmission = transmissionrpc(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            torrents = transmission.get_torrents()
            torrent_list = [
                {
                    'name': torrent.name,
                    'progress': round(torrent.progress, 2),
                    'state': torrent.status,
                    'size': f"{torrent.total_size / (1024 * 1024):.2f} MB"
                }
                for torrent in torrents
            ]
            return render_template('status.html', torrents=torrent_list)
        elif DOWNLOAD_CLIENT == 'qbittorrent':
            qb = Client(host=DL_HOST, port=DL_PORT, username=DL_USERNAME, password=DL_PASSWORD)
            qb.auth_log_in()
            torrents = qb.torrents_info(category=DL_CATEGORY)
            torrent_list = [
                {
                    'name': torrent.name,
                    'progress': round(torrent.progress * 100, 2),
                    'state': torrent.state,
                    'size': f"{torrent.total_size / (1024 * 1024):.2f} MB"
                }
                for torrent in torrents
            ]
        elif DOWNLOAD_CLIENT == "delugeweb":
            delugeweb = delugewebclient(url=DL_URL, password=DL_PASSWORD)
            delugeweb.login()
            torrents = delugeweb.get_torrents_status(
                filter_dict={"label": DL_CATEGORY},
                keys=["name", "state", "progress", "total_size"],
            )
            torrent_list = [
                {
                    "name": torrent["name"],
                    "progress": round(torrent["progress"], 2),
                    "state": torrent["state"],
                    "size": f"{torrent['total_size'] / (1024 * 1024):.2f} MB",
                }
                for k, torrent in torrents.result.items()
            ]
        else:
            return jsonify({'message': 'Unsupported download client'}), 400
        return render_template('status.html', torrents=torrent_list)
    except Exception as e:
        logger.exception("Failed to fetch torrent status: %s", e)
        return jsonify({'message': f"Failed to fetch torrent status: {e}"}), 500



if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5078)
