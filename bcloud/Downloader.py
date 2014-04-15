
# Copyright (C) 2014 LiuLang <gsushzhsosgsu@gmail.com>
# Use of this source code is governed by GPLv3 license that can be found
# in http://www.gnu.org/licenses/gpl-3.0.html

import os
import threading

from gi.repository import GLib
from gi.repository import GObject
import urllib3

from bcloud.const import State
from bcloud import pcs

CHUNK = 2 ** 18         # 256k 
RETRIES = 5             # 下载数据出错时重试的次数
THRESHOLD_TO_FLUSH = 5  # 磁盘写入数据次数超过这个值时, 就进行一次同步.

(NAME_COL, PATH_COL, FSID_COL, SIZE_COL, CURRSIZE_COL, LINK_COL,
    ISDIR_COL, SAVENAME_COL, SAVEDIR_COL, STATE_COL, STATENAME_COL,
    HUMANSIZE_COL, PERCENT_COL) = list(range(13))


class Downloader(threading.Thread, GObject.GObject):
    '''后台下载的线程, 每个任务应该对应一个Downloader对象.

    当程序退出时, 下载线程会保留现场, 以后可以继续下载.
    断点续传功能基于HTTP/1.1 的Range, 百度网盘对它有很好的支持.
    '''

    fh = None
    red_url = ''
    flush_count = 0

    __gsignals__ = {
            'received': (GObject.SIGNAL_RUN_LAST,
                # fs-id, current-size
                GObject.TYPE_NONE, (str, GObject.TYPE_INT64)),
            'downloaded': (GObject.SIGNAL_RUN_LAST, 
                # fs_id
                GObject.TYPE_NONE, (str, )),
            'disk-error': (GObject.SIGNAL_RUN_LAST,
                # fs_id
                GObject.TYPE_NONE, (str, )),
            'network-error': (GObject.SIGNAL_RUN_LAST,
                # fs_id
                GObject.TYPE_NONE, (str, )),
            }

    def __init__(self, parent, row, cookie, tokens):
        threading.Thread.__init__(self)
        GObject.GObject.__init__(self)

        self.parent = parent
        self.cookie = cookie
        self.tokens = tokens

        self.row = row[:]  # 复制一份

        self.pool = urllib3.PoolManager()

    def init_files(self):
        row = self.row
        if not os.path.exists(self.row[SAVEDIR_COL]):
            os.makedirs(row[SAVEDIR_COL], exist_ok=True)
        self.filepath = os.path.join(row[SAVEDIR_COL], row[SAVENAME_COL]) 
        if os.path.exists(self.filepath):
            curr_size = os.path.getsize(self.filepath)
            if curr_size == row[SIZE_COL]:
                self.finished()
                return
            elif curr_size < row[SIZE_COL]:
                if curr_size == row[CURRSIZE_COL]:
                    self.fh = open(self.filepath, 'ab')
                elif curr_size < row[CURRSIZE_COL]:
                    self.fh = open(self.filepath, 'ab')
                    row[CURRSIZE_COL] = curr_size
                else:
                    if 0 < row[CURRSIZE_COL]:
                        self.fh = open(self.filepath, 'ab')
                        self.fh.seek(row[CURRSIZE_COL])
                    else:
                        self.fh = open(self.filepath, 'wb')
                        self.row[CURRSIZE_COL] = 0
            else:
                self.fh = open(self.filepath, 'wb')
                self.row[CURRSIZE_COL] = 0
        else:
            self.fh = open(self.filepath, 'wb')
            self.row[CURRSIZE_COL] = 0


    def destroy(self):
        '''自毁'''
        self.pause()

    def run(self):
        '''实现了Thread的方法, 线程启动入口'''
        self.init_files()
        if self.fh:
            self.get_download_link()

    def get_download_link(self):
        meta = pcs.get_metas(self.cookie, self.tokens, self.row[PATH_COL])
        if not meta or meta['errno'] != 0 or 'info' not in meta:
            self.emit('network-error', self.row[FSID_COL])
        else:
            dlink = meta['info'][0]['dlink']
            red_url, req_id = pcs.get_download_link(self.cookie, dlink)
            if not req_id:
                self.emit('network-error', self.row[FSID_COL])
            else:
                self.red_url = red_url
                self.download()

    def download(self):
        while self.row[STATE_COL] == State.DOWNLOADING:
            range_ = self.get_range()
            if range_:
                self.request_bytes(range_)
        self.close_file()

    def pause(self):
        '''暂停下载任务'''
        self.row[STATE_COL] = State.PAUSED
        self.close_file()

    def stop(self):
        '''停止下载, 并删除之前下载的片段'''
        self.row[STATE_COL] = State.CANCELED
        self.close_file()
        os.remove(self.filepath)

    def close_file(self):
        if self.fh and not self.fh.closed:
            self.fh.flush()
            self.fh.close()
            self.fh = None

    def finished(self):
        self.row[STATE_COL] = State.FINISHED
        self.emit('downloaded', self.row[FSID_COL])
        self.close_file()

    def get_range(self):
        if self.row[CURRSIZE_COL] >= self.row[SIZE_COL]:
            self.finished()
            return None
        start = self.row[CURRSIZE_COL]
        stop = min(start + CHUNK, self.row[SIZE_COL])
        return (start, stop)

    def request_bytes(self, range_):
        resp = self.pool.urlopen('GET', self.red_url, headers={
            'Range': 'bytes={0}-{1}'.format(range_[0], range_[1]-1),
            'Connection': 'Keep-Alive',
            #'Cookie': self.cookie.header_output(),
            })
        for _ in range(RETRIES):
            try:
                self.write_bytes(range_, resp.data)
                return
            except OSError as e:
                # TODO
                pass
        self.emit('network-error', self.row[FSID_COL])

    def write_bytes(self, range_, block):
        if not self.fh or self.row[STATE_COL] != State.DOWNLOADING:
            return
        self.row[CURRSIZE_COL] = range_[1]
        self.emit('received', self.row[FSID_COL], self.row[CURRSIZE_COL])
        self.fh.write(block)
        if self.flush_count >= THRESHOLD_TO_FLUSH:
            self.fh.flush()
            self.flush_count = 0

GObject.type_register(Downloader)
