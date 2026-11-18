import json
import sys

from . import config, harvest, pipeline


def main():
    """默认跑原来的扁平 fixtures；``--sources`` 时跑多来源分页采集。"""
    if "--sources" in sys.argv[1:]:
        sources = harvest.load_sources(config.SOURCES_FIXTURE)
        print(json.dumps(pipeline.run_sources(sources), ensure_ascii=False))
        return
    print(json.dumps(pipeline.run(), ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
