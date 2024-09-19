import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
from io import BytesIO
import matplotlib.pyplot as plt
import os
import numpy as np
import datetime
import easyocr
import logging
import threading
import time
from typing import Dict, List
import argparse

# Retrieve, process and save buoycam images from the NOAA buoycam website

BUOYCAM_LIST_URL = "https://www.ndbc.noaa.gov/buoycams.php"
BUOYCAM_IMAGE_FILE_URL_BASE = "https://www.ndbc.noaa.gov/images/buoycam/"
OBSERVATION_URL = "https://www.ndbc.noaa.gov/data/realtime2"
BUOYCAM_IMAGE_ROW_LENGTH = 6
IMAGE_WIDTH = 2880
IMAGE_HEIGHT = 300
SUB_IMAGE_WIDTH = 480
SUB_IMAGE_HEIGHT = 270

FRACTION_BLACK_THRESHOLD = 0.85

MISSING_DATA_INDICATOR = "MM"


# Rate limiting lock to be nice to the NOAA buoycam website
request_rate_limit_lock = threading.Lock()
# Max requests per second
MAX_REQUESTS_PER_SECOND = 30
# Number of worker threads
NUM_WORKER_THREADS = 30


LOGGER = logging.getLogger(__name__)

# pylint: disable=missing-function-docstring


def get_json(url):
    response = requests.get(url, timeout=10)
    return response.json()


class OCR:
    """
    Extracts text from images using OCR
    """

    def __init__(self):
        self.reader = easyocr.Reader(["en"])

    def get_all_text_from_image(self, image):
        # Get all text from the image
        results = self.reader.readtext(np.array(image))
        if len(results) == 0:
            return None

        # Concatenate all the text results
        text_result = ""
        for result in results:
            text_result += result[1]

        return text_result

    def get_angle_from_image(self, image):
        # Get angle in degrees from the image
        results = self.reader.readtext(np.array(image))

        if len(results) == 0:
            return None

        # There should be at least 1 digit and 1 degree symbol
        text_result = results[0][1]
        if len(text_result) < 2:
            return None

        # Remove the last character (degree symbol) from the result
        first_result = text_result[:-1]
        # verify that the result is a number
        if not first_result.isnumeric():
            return None

        angle = int(first_result)

        if angle < 0 or angle > 360:
            return None

        return angle


class BuoyImageRequest:
    """
    BuoyImageRequest is a class that encapsulates the information needed for a request to get a buoycam image.
    """

    def __init__(self, station_id, station_tag, desciption, date, output_dir="images"):
        self.id: str = station_id
        self.tag: str = station_tag
        self.description: str = desciption
        self.date: datetime.datetime = date
        self.output_root_dir: str = output_dir

    def date_string(self):
        return self.date.strftime("%Y_%m_%d_%H%M")

    def image_name(self):
        return f"{self.tag}_{self.date_string()}"

    def image_filename(self):
        return f"{self.image_name()}.jpg"

    def url(self):
        return f"{BUOYCAM_IMAGE_FILE_URL_BASE}{self.image_filename()}"

    def save_directory(self):
        return f"{self.output_root_dir}/{self.id}/{self.date_string()}"
    
    def save_image_full_path(self, postfix=""):
        return f"{self.save_directory()}/{self.image_name()}{f"_{postfix}" if postfix else ""}.jpg"

    def __str__(self):
        return f"BuoyImageRequest(id={self.id}, tag={self.tag}, date={self.date})"

def extract_table_data(url: str) -> list:
    response = requests.get(url, timeout=10)

    # verify that the request was successful
    if response.status_code != 200:
        LOGGER.debug("Failed to get table data from %s due to status code %s", url, response.status_code)
        return None

    data = response.text
    lines = data.split("\n")

    # Verify that the file has at least 3 lines (header, units, and data)
    if len(lines) < 3:
        raise ValueError("File does not have enough lines")

    # Extract the header and the first row.
    # Skip the first character in the first row as it is just to denote it's a non data row
    header = lines[0][1:].split()
    # Skip the second line which is the units
    rows = lines[2:]

    result = []
    for row in rows:
        values = row.split()
        if len(values) == 0:
            continue
        entry = {}
        for i, h in enumerate(header):
            entry[h] = values[i]
        result.append(entry)

    return result


def get_float(row: dict, key: str, convert_func=None) -> float:
    if key not in row:
        return None
    if row[key] == MISSING_DATA_INDICATOR:
        return None

    if convert_func is None:
        return float(row[key])

    return convert_func(float(row[key]))


def table_row_to_db_entry(row, station_id):
    return {
        "station_id": station_id,
        "timestamp": f'{row["YY"]}_{row["MM"]}_{row["DD"]}_{row["hh"]}{row["mm"]}',
        "wind_speed_kts": get_float(row, "WSPD", mps_to_kts),
        "wind_direction_deg": get_float(row, "WDIR"),
        "gust_speed_kts": get_float(row, "GST", mps_to_kts),
        "wave_height_m": get_float(row, "WVHT"),
        "dominant_wave_period_s": get_float(row, "DPD"),
        "average_wave_period_s": get_float(row, "APD"),
        "mean_wave_direction_deg": get_float(row, "MWD"),
        "atmospheric_pressure_hpa": get_float(row, "PRES"),
        "air_temperature_c": get_float(row, "ATMP"),
        "water_temperature_c": get_float(row, "WTMP"),
        "dewpoint_temperature_c": get_float(row, "DEWP"),
        "visibility_m": get_float(row, "VIS", nmi_to_m),
        "pressure_tendency_hpa": get_float(row, "PTDY"),
        "tide_m": get_float(row, "TIDE"),
    }


def mps_to_kts(mps: float) -> float:
    # Convert meters per second to knots
    return mps * 1.94384


def nmi_to_m(nmi: float) -> float:
    # Convert nautical miles to meters
    return nmi * 1852


class Observation:
    """
    Observation data for a buoy at a specific time.
    """

    def __init__(
        self,
        timestamp,
        wind_speed_kts,
        wind_direction_deg,
        gust_speed_kts,
        wave_height_m,
        dominant_wave_period_s,
        average_wave_period_s,
        mean_wave_direction_deg,
        atmospheric_pressure_hpa,
        air_temperature_c,
        water_temperature_c,
        dewpoint_temperature_c,
        visibility_m,
        pressure_tendency_hpa,
        tide_m,
    ):
        self.timestamp: str = timestamp
        self.wind_speed_kts: float = wind_speed_kts
        self.wind_direction_deg: float = wind_direction_deg
        self.gust_speed_kts: float = gust_speed_kts
        self.wave_height_m: float = wave_height_m
        self.dominant_wave_period_s: float = dominant_wave_period_s
        self.average_wave_period_s: float = average_wave_period_s
        self.mean_wave_direction_deg: float = mean_wave_direction_deg
        self.atmospheric_pressure_hpa: float = atmospheric_pressure_hpa
        self.air_temperature_c: float = air_temperature_c
        self.water_temperature_c: float = water_temperature_c
        self.dewpoint_temperature_c: float = dewpoint_temperature_c
        self.visibility_m: float = visibility_m
        self.pressure_tendency_hpa: float = pressure_tendency_hpa
        self.tide_m: float = tide_m

    def __str__(self):
        return f"Observation(timestamp={self.timestamp}, wind_speed_kts={self.wind_speed_kts}, wind_direction_deg={self.wind_direction_deg}, gust_speed_kts={self.gust_speed_kts}, wave_height_m={self.wave_height_m}, dominant_wave_period_s={self.dominant_wave_period_s}, average_wave_period_s={self.average_wave_period_s}, mean_wave_direction_deg={self.mean_wave_direction_deg}, atmospheric_pressure_hpa={self.atmospheric_pressure_hpa}, air_temperature_c={self.air_temperature_c}, water_temperature_c={self.water_temperature_c}, dewpoint_temperature_c={self.dewpoint_temperature_c}, visibility_m={self.visibility_m}, pressure_tendency_hpa={self.pressure_tendency_hpa}, tide_m={self.tide_m})"


def get_observation_str_for_file(station_id: str, description: str, lat_deg: float, lon_deg: float, observation: Observation, angle: int = None) -> str:
    s = f"id {station_id}\n"
    s += f"description {description}\n"
    s += f"timestamp {observation.timestamp}\n"
    s += f"lat_deg {lat_deg}\n"
    s += f"lon_deg {lon_deg}\n"
    s += f"first_image_bearing_deg {angle}\n"
    s += f"wind_speed_kts {observation.wind_speed_kts}\n"
    s += f"wind_direction_deg {observation.wind_direction_deg}\n"
    s += f"gust_speed_kts {observation.gust_speed_kts}\n"
    s += f"wave_height_m: {observation.wave_height_m}\n"
    s += f"dominant_wave_period_s {observation.dominant_wave_period_s}\n"
    s += f"average_wave_period_s {observation.average_wave_period_s}\n"
    s += f"mean_wave_direction_deg {observation.mean_wave_direction_deg}\n"
    s += f"atmospheric_pressure_hpa {observation.atmospheric_pressure_hpa}\n"
    s += f"air_temperature_c {observation.air_temperature_c}\n"
    s += f"water_temperature_c {observation.water_temperature_c}\n"
    s += f"dewpoint_temperature_c {observation.dewpoint_temperature_c}\n"
    s += f"visibility_m {observation.visibility_m}\n"
    s += f"pressure_tendency_hpa {observation.pressure_tendency_hpa}\n"
    s += f"tide_m {observation.tide_m}\n"
    return s


class BuoyData:
    """
    A collection of observations for a buoy.
    """

    def __init__(self, station_id: str, description: str = "", lat_deg: float = None, lon_deg: float = None):
        self.id = station_id
        # key is the date string, value is the observation
        self.observations: Dict[str, Observation] = {}
        self.lat_deg = lat_deg
        self.lon_deg = lon_deg
        self.description = description

    def add_observation(self, observation: Observation):
        self.observations[observation.timestamp] = observation

    def has_observation(self, timestamp):
        return timestamp in self.observations

    def get_observation(self, timestamp):
        if self.has_observation(timestamp):
            return self.observations[timestamp]
        return None


def table_row_to_observation(row):
    # Must have a timestamp
    if "YY" not in row or "MM" not in row or "DD" not in row or "hh" not in row or "mm" not in row:
        return None
    return Observation(
        timestamp=f'{row["YY"]}_{row["MM"]}_{row["DD"]}_{row["hh"]}{row["mm"]}',
        wind_speed_kts=get_float(row, "WSPD", mps_to_kts),
        wind_direction_deg=get_float(row, "WDIR"),
        gust_speed_kts=get_float(row, "GST", mps_to_kts),
        wave_height_m=get_float(row, "WVHT"),
        dominant_wave_period_s=get_float(row, "DPD"),
        average_wave_period_s=get_float(row, "APD"),
        mean_wave_direction_deg=get_float(row, "MWD"),
        atmospheric_pressure_hpa=get_float(row, "PRES"),
        air_temperature_c=get_float(row, "ATMP"),
        water_temperature_c=get_float(row, "WTMP"),
        dewpoint_temperature_c=get_float(row, "DEWP"),
        visibility_m=get_float(row, "VIS", nmi_to_m),
        pressure_tendency_hpa=get_float(row, "PTDY"),
        tide_m=get_float(row, "TIDE"),
    )


def get_observation_data(station_id) -> BuoyData:

    with request_rate_limit_lock:
        # Simple rate limiting mechanism to be nice to the NOAA website
        time.sleep(1 / MAX_REQUESTS_PER_SECOND)

    url = f"{OBSERVATION_URL}/{station_id}.txt"
    observation_data = extract_table_data(url)
    if observation_data is None:
        LOGGER.warning("Failed to get buoy data for buoy %s", station_id)
        return None
    data = BuoyData(station_id)
    for row in observation_data:
        observation = table_row_to_observation(row)
        if observation is not None:
            data.add_observation(observation)
    return data


def extract_date_string(filename):
    # Find the position of the first underscore
    start_index = filename.find("_") + 1
    if start_index == -1:
        return None
    # The end index is where ".jpg" starts
    end_index = filename.find(".jpg")
    if end_index == -1:
        return None
    # Extract the substring from start_index to end_index
    return filename[start_index:end_index]


def date_string_to_date(date_string):
    # Extract the year, month, and day from the date string. All times are in UTC.
    # Expected date format "YYYY_MM_DD_HHMM"
    if len(date_string) != 15:
        return None

    year = int(date_string[:4])
    month = int(date_string[5:7])
    day = int(date_string[8:10])
    hour = int(date_string[11:13])
    minute = int(date_string[13:15])
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc)


def get_image_file(url):
    response = requests.get(url, timeout=10)
    if response.status_code != 200:
        LOGGER.debug("Failed to get image file at %s", url)
        return None
    LOGGER.debug("Image file retrieved from %s ", url)
    return Image.open(BytesIO(response.content))


def date_to_date_string(date):
    return date.strftime("%Y_%m_%d_%H%M")


def split_image(img):
    sub_images = []
    for i in range(BUOYCAM_IMAGE_ROW_LENGTH):
        left = i * SUB_IMAGE_WIDTH
        sub_img = img.crop((left, 0, left + SUB_IMAGE_WIDTH, SUB_IMAGE_HEIGHT))
        sub_images.append(sub_img)
    return sub_images


# the fraction of the image that is black
def fraction_black(img):
    img_array = np.array(img.convert("L")).flatten()
    return np.count_nonzero(img_array == 0) / len(img_array)


def get_angle_from_image(img: Image, ocr_reader: OCR):
    assert img.width == IMAGE_WIDTH and img.height == IMAGE_HEIGHT
    angle_crop = img.crop((150, img.height - 30, 250, img.height))
    # plt.imshow(angle_crop)
    # plt.axis("off")
    # plt.show()

    # Extract the angle from the image
    return ocr_reader.get_angle_from_image(angle_crop)


def fetch_and_process_image(request: BuoyImageRequest, buoy_data: BuoyData, output_dir:str, ocr_reader: OCR = None) -> bool:

    # Simple rate limiting mechanism to be nice to the NOAA buoycam website
    with request_rate_limit_lock:
        time.sleep(1 / MAX_REQUESTS_PER_SECOND)

    LOGGER.debug("\tImage filename: %s", request.image_filename())

    # Get the image file
    img = get_image_file(request.url())
    if img is None:
        LOGGER.debug("\t%s failed", request)
        return False

    # Verify the image size
    if img.width != IMAGE_WIDTH or img.height != IMAGE_HEIGHT:
        LOGGER.warning("\t%d: image has invalid dimensions %dx%d", request, img.width, img.height)
        return False

    # Extract the sub images from the full image
    sub_images = split_image(img)

    # make the folder if it doesn't exist
    # img_dir = request.save_directory()
    if not os.path.exists(request.save_directory()):
        os.makedirs(request.save_directory())

    # Save the full image
    full_image_path = request.save_image_full_path("full")
    img.save(full_image_path)
    LOGGER.debug("\tFull image saved at %s", full_image_path)

    # Save the sub images
    for i, sub_img in enumerate(sub_images):
        # Check if the sub image is mostly black
        fraction_black_value = fraction_black(sub_img)
        if fraction_black_value > FRACTION_BLACK_THRESHOLD:
            LOGGER.debug("\t\tSub image %s is %.2f%% black, skipping", i, fraction_black_value * 100)
            continue

        LOGGER.debug("\t\tSub image %i is %.2f%% black", i, fraction_black_value * 100)

        sub_img_path = request.save_image_full_path(str(i))
        sub_img.save(sub_img_path)
        LOGGER.debug("\t\tSub image %d saved at %s", i, sub_img_path)

    bearing_of_first_image = None
    if ocr_reader is not None:
        bearing_of_first_image = get_angle_from_image(img, ocr_reader)
        if bearing_of_first_image is None:
            LOGGER.warning("\tFailed to extract angle from buoycam %s", request.id)
        else:
            LOGGER.debug("\tExtracted angle: %d", bearing_of_first_image)


    if buoy_data.has_observation(request.date_string()):
        observation = buoy_data.get_observation(request.date_string())
        LOGGER.debug("\tObservation for buoy %s at %s: %s", request, request.date_string(), observation)
        # Save the observation data
        observation_path = f"{request.save_directory()}/observation.txt"
        with open(observation_path, "w", encoding="utf-8") as file:
            file.write(get_observation_str_for_file(request.id, buoy_data.description, buoy_data.lat_deg, buoy_data.lon_deg, observation, bearing_of_first_image))
    else:
        LOGGER.warning("\tNo observation data for buoy %s at %s", request.id, request.date_string())

    return True

def generate_requests(buoy_cam_list, output_dir, hours_in_past) -> list[BuoyImageRequest]:
    image_requests = []
    for buoy_cam in buoy_cam_list:
        if "id" not in buoy_cam or "name" not in buoy_cam or "img" not in buoy_cam:
            LOGGER.error("Buoycam does not have all required fields. Invalid dictionary: %s", buoy_cam)
            continue

        station_id = buoy_cam["id"]
        if station_id is None:
            LOGGER.error("Buoycam does not have an id. Invalid dictionary: %s", buoy_cam)
            continue

        img_filename = buoy_cam["img"]
        if img_filename is None:
            LOGGER.warning("Buoycam %s: does not have an image", station_id)
            continue

        # before the first '_"
        station_tag = img_filename.split("_")[0]

        img_datetime_string = extract_date_string(img_filename)

        if img_datetime_string is None:
            LOGGER.error("Buoycam %s: has an invalid image filename: %s", station_id, img_filename)
            continue

        latest_img_date = date_string_to_date(img_datetime_string)

        if latest_img_date is None:
            LOGGER.error("Buoycam %s: has an invalid image date: %s", station_id, img_datetime_string)
            continue

        image_dates = [
            latest_img_date - datetime.timedelta(minutes=10 * i) for i in range(hours_in_past * 6)
        ]

        image_requests.extend([BuoyImageRequest(station_id, station_tag, buoy_cam["name"] , date, output_dir) for date in image_dates])
    return image_requests


def nowutc():
    return datetime.datetime.now(datetime.UTC)


def main():
    parser = argparse.ArgumentParser(description="A script to fetch, process, and save buoycam images and observation data from the NOAA website")

    parser.add_argument("--output", type=str, help="Output directory for images and observation data", default="images")

    parser.add_argument("--log", type=str, help="Log level", default="INFO")

    parser.add_argument("--log-file", type=str, help="Log file", default=None)

    parser.add_argument("--hours", type=int, help="Number of hours in the past to retrieve images for", default=24)

    # Parse the arguments
    args = parser.parse_args()

    # setup the loggers
    LOGGER.setLevel(args.log)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    LOGGER.addHandler(console_handler)
    

    if args.log_file is not None:
        file_handler = logging.FileHandler(args.log_file)
        file_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)

    LOGGER.info("Starting buoycam fetcher, getting images for the last %d hours", args.hours)
    timer = nowutc()

    buoy_cam_list = get_json(BUOYCAM_LIST_URL)

    LOGGER.info("Found %d buoycams", len(buoy_cam_list))

    # Observation look up object
    buoy_data_lookup: Dict[str, BuoyData] = {}
    with ThreadPoolExecutor(max_workers=NUM_WORKER_THREADS) as executor:
        futures_to_input = {
            executor.submit(get_observation_data, buoy["id"]): buoy for buoy in buoy_cam_list if "id" in buoy
        }

        for future in as_completed(futures_to_input):
            result = future.result()
            buoy = futures_to_input[future]
            LOGGER.info("Completed observation request for %s, success: %s", buoy["id"], result is not None)

            if result is not None:
                result.description = buoy["name"] if "name" in buoy else None
                result.lat_deg = float(buoy["lat"]) if "lat" in buoy else None
                result.lon_deg = float(buoy["lng"]) if "lng" in buoy else None
                
                buoy_data_lookup[result.id] = result

    LOGGER.debug("Completed all observation requests in %s", nowutc() - timer)

    LOGGER.info("Retrieved observation data for %s buoycams", len(buoy_data_lookup))

    ocr_reader = OCR()

    # Generate image requests for all buoycams that have observation data
    image_requests = generate_requests([b for b in buoy_cam_list], args.output, args.hours)

    # only request images that we have observation data for
    filtered_requests = []
    for ir in image_requests:
        if ir.id not in buoy_data_lookup:
            LOGGER.warning("Buoy %s does not have observation data", ir.id)

        if not buoy_data_lookup[ir.id].has_observation(ir.date_string()):
            LOGGER.warning("Buoy %s does not have observation data for %s", ir.id, ir.date_string())
        filtered_requests.append(ir)

    LOGGER.info("Generated %s image requests", len(filtered_requests))

    timer = nowutc()
    results = []

    with ThreadPoolExecutor(max_workers=NUM_WORKER_THREADS) as executor:

        futures_to_request = {}

        for request in filtered_requests:
            future = executor.submit(fetch_and_process_image, request, buoy_data_lookup[request.id], ocr_reader)
            futures_to_request[future] = request

        for future in as_completed(futures_to_request):
            request = futures_to_request[future]
            results.append((request, future.result()))
            LOGGER.info("Completed %s, success: %s", request, future.result())

    LOGGER.info("Completed all image requests in %s", nowutc() - timer)


if __name__ == "__main__":
    main()
