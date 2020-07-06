#!/bin/sh


# From https://developer.spotify.com/dashboard/applications
export USER_ID='esauvisky'
export CLIENT_ID='593f82d226494d6185dae5a6a0c5c396'
export CLIENT_SECRET='40a2090b339b4822932b881e087d8d1f'

export DEVICE_IP='192.168.0.51'
export HOST='0.0.0.0'
export HOST_PORT='8080'
export DEVICE_PORT='42424'
export LEDS=60

python app.py
