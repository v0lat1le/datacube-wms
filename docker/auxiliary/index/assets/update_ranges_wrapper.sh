#!/bin/bash
# Wraps the update_ranges.sh script by passing in environment variables
# from the Docker's environment variables

indexing/update_ranges.sh -b "$DC_S3_INDEX_BUCKET" -p "$DC_S3_INDEX_PREFIX" -s "$DC_S3_INDEX_SUFFIX" -y "$DC_INDEX_YAML_SAFETY" -d "$DC_RANGES_PRODUCT" -u "$DC_SOURCES_POLICY"