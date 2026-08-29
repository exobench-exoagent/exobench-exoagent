"""
Scrape exoplanet observations from the MicroObservatory image archive.

This script queries the public MicroObservatory image directory for entries
whose object category is "ExoPlanets", parses the returned HTML results table,
groups consecutive image rows by observed object, and downloads each group's
FITS images into local `planet_<index>` folders. Calibration rows are handled
separately by placing their FITS files under a `darks` subfolder for the
current planet folder.
"""

import os
import requests
from bs4 import BeautifulSoup

COL_NAMES = ["Image Filename", "Date & Time", "Open JS9/4L", "FITS Image", "Field of View", "Exposure Time (sec)", "Filter", "Object", "Telescope", "Site", "User", "Size", "Metadata", "Weather"]

def download_file(url, save_folder):

    # Determine the filename from the URL
    filename = url.split('/')[-1].replace(" ", "_")
    filepath = os.path.join(save_folder, filename)

    print(f"Downloading: {url}")
    
    # Send a GET request to the URL, stream the data for large files
    try:
        with requests.get(url, stream=True) as r:
            r.raise_for_status()  # Raise an exception for bad status codes (4XX/5XX)
            if r.status_code == 200:
                with open(filepath, 'wb') as f:
                    # Write the file in chunks to efficiently handle large files
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk: # Filter out keep-alive chunks
                            f.write(chunk)
                print(f"Successfully downloaded and saved to: {os.path.abspath(filepath)}")
            else:
                print(f"Download failed: status code {r.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"An error occurred: {e}")

# Request from MicroObservatory Archive
url = """https://waps.cfa.harvard.edu/microobservatory/MOImageDirectory/
ImageDirectory.php?SortBy=&SortPos=&SearchFor=ExoPlanets&Type=Object&Sor
tRange=30""".replace("\n", "")
html = requests.get(url).text

# Parse via BeautifulSoup
soup = BeautifulSoup(html, "html.parser")
table = soup.find_all("table")[1]

# Store the rows of images of exoplanets, to be aggregated
# per observation it corresponds to
rows = []

for tr in table.find_all("tr"):

    currentRow = tr.find_all(["td", "th"])

    rowEntry = []
    for cell in currentRow:
        if cell.get_text(strip=True) == "":
            link = cell.find("a")
            if link and link.get("href"):
                rowEntry.append(link.get("href"))
            else:
                rowEntry.append("<NO LINK OR TEXT FOUND>")   # fallback
        else:
            rowEntry.append(cell.get_text(strip = True))

    rows.append(rowEntry)

chunks = []
current_chunk = []
current_object = None

# Skip header row (rows[0]) since it's just column names
for row in rows[1:]:

    obj = row[7]

    if current_object is None: # First row
        current_object = obj
        current_chunk.append(row)
    elif obj == current_object: # Same object as current run
        current_chunk.append(row)
    else: # Object changed => close current chunk and start a new one
        chunks.append(current_chunk)
        current_chunk = [row]
        current_object = obj

# Ensure to place last chunk
if current_chunk:
    chunks.append(current_chunk)

actualChunks = []
for observation in chunks:
    actualChunks.append(observation)

objectIndex = 0

for i, chunk in enumerate(actualChunks):

    images = []

    for eachImage in chunk:
        entry = dict()
        for ind in range(len(eachImage)):
            entry[COL_NAMES[ind]] = eachImage[ind]
        images.append(entry)

    objectName = images[0]['Object']
        
    print(f"Observation #{objectIndex} | Exoplanet Name: {objectName}, # of Images = {len(images)}")

    directory_path = f'./planet_{objectIndex}'
    os.makedirs(directory_path, exist_ok=True)

    directory_path_darks = directory_path + '/darks'
    os.makedirs(directory_path_darks, exist_ok=True)

    if objectName == 'Calibration':
        for eachImage in images:
            download_file(eachImage["FITS Image"], directory_path_darks)
        continue

    for eachImage in images:
        download_file(eachImage["FITS Image"], directory_path)

    objectIndex += 1
