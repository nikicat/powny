FROM nikicat/ubuntu-pypy3

ADD etc/gns.d /etc/gns.d
ADD . /root/gns
RUN cd /root/gns && pypy3 setup.py install