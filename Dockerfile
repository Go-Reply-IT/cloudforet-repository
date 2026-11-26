FROM cloudforet/repository:1.12.5

USER root

COPY src/spaceone/repository/connector/registry_connector.py \
    /usr/local/lib/python3.8/site-packages/spaceone_repository-1.12.5-py3.8.egg/spaceone/repository/connector/registry_connector.py

RUN python3 -m py_compile \
    /usr/local/lib/python3.8/site-packages/spaceone_repository-1.12.5-py3.8.egg/spaceone/repository/connector/registry_connector.py