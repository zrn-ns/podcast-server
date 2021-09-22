#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import yaml
import logging
import eyed3
from datetime import datetime

class FeedGenerator:
    @staticmethod
    def generate():
        file_name = "/usr/local/apache2/htdocs/index.html"
        try:
            file = open(file_name, 'a')
            datetime_str = datetime.now().strftime('%Y/%m/%d %H:%M:%S')
            file.write(datetime_str)
        except Exception as e:
            print(e)
        finally:
            file.close()

if __name__ == "__main__":
    FeedGenerator.generate()
