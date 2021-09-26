#!/usr/bin/env python
# -*- coding: utf-8 -*-

from feed_generator import FeedGenerator
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = BlockingScheduler()
scheduler.add_job(FeedGenerator.generate, CronTrigger.from_crontab('*/15 * * * *'))

scheduler.start()
