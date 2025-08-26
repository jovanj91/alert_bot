#!/bin/bash

cd /home/regional/data1/alert_bot
nohup python3 main.py > output.log 2>&1 &
