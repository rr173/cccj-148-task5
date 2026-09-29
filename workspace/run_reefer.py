#!/usr/bin/env python3
"""启动冷藏箱岸电接入与断电迁移服务：  python3 run_reefer.py [port] [data_dir]"""
import os
import sys

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8095
    if len(sys.argv) > 2:
        os.environ["REEFER_DATA"] = os.path.abspath(sys.argv[2])
    from reefer.server import main
    main()
