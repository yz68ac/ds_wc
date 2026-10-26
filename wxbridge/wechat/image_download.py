"""Bounded AT-SPI download of a visually matched, DB-verified image; no DB/API access."""
from __future__ import annotations

from contextlib import contextmanager
import math
import os
from pathlib import Path
import stat
import time


class DownloadError(RuntimeError):
    pass


def similarity(a, b):
    """RGB MAE plus spatial correlation; flat color cannot prove an image match."""
    if not a or len(a) != len(b) or len(a) % 3:
        raise DownloadError('download_invalid_pixels')
    means_a = [sum(a[c::3])/(len(a)//3) for c in range(3)]
    means_b = [sum(b[c::3])/(len(b)//3) for c in range(3)]
    va = sum((v-means_a[i%3])**2 for i,v in enumerate(a))
    vb = sum((v-means_b[i%3])**2 for i,v in enumerate(b))
    correlation = sum((x-means_a[i%3])*(y-means_b[i%3]) for i,(x,y) in enumerate(zip(a,b)))/max(1,math.sqrt(va*vb))
    return dict(mae=sum(abs(x-y) for x,y in zip(a,b))/len(a),
                correlation=correlation, source_std=math.sqrt(va/len(a)))


def unique_match(candidates):
    ranked = sorted(((min(c['cover']['mae'],c['contain']['mae']),i,c) for i,c in enumerate(candidates)),key=lambda v:v[0])
    accepted = [c for c in candidates if any(c[k]['mae'] < 10 and c[k]['correlation'] > .98
                and c[k]['source_std'] > 12 for k in ('cover','contain'))]
    if len(accepted) != 1:
        return None
    if len(ranked)>1 and ranked[1][0] <= ranked[0][0]*3+10:
        return None
    return accepted[0]


@contextmanager
def download_lock(cfg, deadline):
    import fcntl
    from wxbridge.storage.memoryfs import require_memory_directory
    path = Path(cfg['queue']['file']).parent/'image-download.lock'
    require_memory_directory(str(path.parent))
    fd = os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        s=os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.geteuid() or s.st_mode&0o077:
            raise DownloadError('download_unsafe_lock')
        while True:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise DownloadError('download_busy')
                time.sleep(.1)
        yield
    finally:
        os.close(fd)


class DesktopImages:
    def __init__(self,cfg,deadline):
        import gi
        gi.require_version('Gdk','3.0');gi.require_version('GdkPixbuf','2.0')
        from gi.repository import Gdk,GdkPixbuf
        from wxbridge.wechat.ui import A11y,X11
        self.Gdk,self.Pixbuf=Gdk,GdkPixbuf
        self.ui,self.x=A11y(cfg['ui']),X11(cfg['ui'])
        self.deadline=deadline
        self.pid=str(self.ui.require_app().get_process_id())
        self.created=[]

    def check(self):
        if time.monotonic()>=self.deadline:raise DownloadError('download_timeout')

    def visible(self,root):
        for node,role,name,bounds in self.ui.walk(root,max_depth=35,max_nodes=4500):
            self.check()
            if bounds and node.get_state_set().contains(self.ui.Atspi.StateType.SHOWING):
                yield node,role,name,bounds

    def window(self,frame):
        self.check();b=self.ui._bounds(frame);found=[]
        if not b:raise DownloadError('download_window_unavailable')
        for win in self.x._xdotool('search','--onlyvisible','--pid',self.pid).stdout.decode().split():
            if not self.x._is_native_window(win):continue
            g=dict(s.split('=',1) for s in self.x._xdotool('getwindowgeometry','--shell',win).stdout.decode().splitlines() if '=' in s)
            if sum(abs(int(g[k])-b[key]) for k,key in [('X','x'),('Y','y'),('WIDTH','width'),('HEIGHT','height')])<=10:found.append(win)
        if len(found)!=1:raise DownloadError('download_window_unavailable')
        return found[0]

    def click(self,frame,bounds,*,double=False):
        self.check();win=self.window(frame)
        self.x._xdotool('windowactivate','--sync',win,timeout=3);self.x._xdotool('windowraise',win)
        time.sleep(.15)
        if self.x._xdotool('getactivewindow').stdout.decode().strip()!=win:
            raise DownloadError('download_window_obstructed')
        self.x.move_pointer(bounds['x']+bounds['width']//2,bounds['y']+bounds['height']//2)
        self.check()
        if self.x.pointer_client()!=win or not self.x._is_native_window(win):
            raise DownloadError('download_window_obstructed')
        self.x._xdotool('click',*(['--repeat','2','--delay','80'] if double else []),'1')

    def ancestor_frame(self,node):
        for _ in range(40):
            if node.get_role_name()=='frame':return node
            node=node.get_parent()
            if node is None:break
        raise DownloadError('download_window_unavailable')

    def frame_with(self,label,role):
        until=min(self.deadline,time.monotonic()+3)
        while True:
            frames=[]
            for node,r,name,_ in self.visible(self.ui.require_app()):
                if r==role and name==label:
                    frame=self.ancestor_frame(node)
                    try:self.window(frame)
                    except DownloadError:continue  # Qt can retain SHOWING nodes after their X window closes.
                    if frame not in frames:frames.append(frame)
            if len(frames)==1:return frames[0]
            if len(frames)>1 or time.monotonic()>=until:raise DownloadError('download_controls_unavailable')
            time.sleep(.1)

    def control(self,frame,names,roles=('button','push button')):
        found=[(node,b) for node,r,name,b in self.visible(frame) if r in roles and name in names]
        if len(found)!=1:raise DownloadError('download_controls_unavailable')
        return found[0]

    def windows(self):
        return set(self.x._xdotool('search','--onlyvisible','--pid',self.pid).stdout.decode().split())

    def open_gallery(self,title):
        main=self.frame_with('Chats','list')
        if self.ui.chat_is_selected(title) is not True:
            item=self.ui.find_chat_item(title)
            if item is None:raise DownloadError('download_chat_unavailable')
            self.click(main,self.ui._bounds(item))
            until=min(self.deadline,time.monotonic()+2)
            while self.ui.chat_is_selected(title) is not True:
                if time.monotonic()>=until:raise DownloadError('download_wrong_chat')
                time.sleep(.1)
        before=self.windows()
        _,b=self.control(main,('Chat History','聊天记录'));self.click(main,b)
        time.sleep(.25)
        self.created.extend(self.windows()-before)
        history=self.frame_with('Media','page tab')
        _,b=self.control(history,('Media','图片与视频','媒体'),roles=('page tab',))
        self.click(history,b);time.sleep(.25)
        return history

    def pixels(self,pixbuf):
        p=pixbuf.scale_simple(20,20,self.Pixbuf.InterpType.BILINEAR)
        data=p.get_pixels();stride=p.get_rowstride();channels=p.get_n_channels()
        return [data[y*stride+x*channels+c] for y in range(20) for x in range(20) for c in range(3)]

    def matches(self,frame,image):
        loader=self.Pixbuf.PixbufLoader.new();loader.write(image);loader.close();source=loader.get_pixbuf()
        w,h=source.get_width(),source.get_height();side=min(w,h)
        center=self.pixels(source.new_subpixbuf((w-side)//2,(h-side)//2,side,side));full=self.pixels(source)
        root=self.Gdk.get_default_root_window();fb=self.ui._bounds(frame);matches=[]
        for _,role,_,b in self.visible(frame):
            if role!='list item' or not 32<=b['width']<=256 or abs(b['width']-b['height'])>2:continue
            if b['x']<fb['x'] or b['y']<fb['y']+150 or b['x']+b['width']>fb['x']+fb['width'] or b['y']+b['height']>fb['y']+fb['height']:continue
            shot=self.Gdk.pixbuf_get_from_window(root,b['x'],b['y'],b['width'],b['height'])
            if shot is None:continue
            scale=min(b['width']/w,b['height']/h);cw=max(1,round(w*scale));ch=max(1,round(h*scale))
            fitted=shot.new_subpixbuf((b['width']-cw)//2,(b['height']-ch)//2,cw,ch)
            matches.append(dict(bounds=b,cover=similarity(center,self.pixels(shot)),contain=similarity(full,self.pixels(fitted))))
        return matches

    def open_matching(self,frame,image):
        for page in range(3):
            candidates=self.matches(frame,image);matched=unique_match(candidates)
            if matched:
                before=self.windows();self.click(frame,matched['bounds'],double=True)
                time.sleep(.25);self.created.extend(self.windows()-before)
                return
            if any(min(c['cover']['mae'],c['contain']['mae'])<10 for c in candidates):
                raise DownloadError('download_match_ambiguous')
            if page<2:
                self.check();b=self.ui._bounds(frame);win=self.window(frame)
                self.x._xdotool('windowactivate',win);self.x.move_pointer(b['x']+b['width']//2,b['y']+b['height']//2)
                if self.x.pointer_client()!=win:raise DownloadError('download_window_obstructed')
                self.x._xdotool('click','--repeat','4','--delay','50','5');time.sleep(.2)
        raise DownloadError('download_match_missing')

    def close(self):
        for win in reversed(self.created):
            try:
                if self.x._is_native_window(win) and self.x._xdotool('getwindowpid',win).stdout.decode().strip()==self.pid:
                    request_close(int(win))
                    until=time.monotonic()+1
                    while win in self.windows() and time.monotonic()<until:time.sleep(.05)
            except Exception:pass


def request_close(window):
    """WM_DELETE_WINDOW asks Qt to close its view; XDestroyWindow leaves stale Qt state."""
    import ctypes as c
    class Data(c.Union):
        _fields_=[('b',c.c_char*20),('s',c.c_short*10),('l',c.c_long*5)]
    class Message(c.Structure):
        _fields_=[('type',c.c_int),('serial',c.c_ulong),('send_event',c.c_int),('display',c.c_void_p),
                  ('window',c.c_ulong),('message_type',c.c_ulong),('format',c.c_int),('data',Data)]
    class Event(c.Union):
        _fields_=[('message',Message),('pad',c.c_long*24)]
    lib=c.CDLL('libX11.so.6')
    lib.XOpenDisplay.argtypes=[c.c_char_p];lib.XOpenDisplay.restype=c.c_void_p
    lib.XInternAtom.argtypes=[c.c_void_p,c.c_char_p,c.c_int];lib.XInternAtom.restype=c.c_ulong
    lib.XSendEvent.argtypes=[c.c_void_p,c.c_ulong,c.c_int,c.c_long,c.POINTER(Event)];lib.XSendEvent.restype=c.c_int
    lib.XFlush.argtypes=[c.c_void_p];lib.XCloseDisplay.argtypes=[c.c_void_p]
    display=lib.XOpenDisplay(None)
    if not display:raise DownloadError('download_display_unavailable')
    try:
        e=Event();e.message.type=33;e.message.send_event=1;e.message.display=display;e.message.window=window
        e.message.message_type=lib.XInternAtom(display,b'WM_PROTOCOLS',0);e.message.format=32
        e.message.data.l[0]=lib.XInternAtom(display,b'WM_DELETE_WINDOW',0)
        if not lib.XSendEvent(display,window,0,0,c.byref(e)):raise DownloadError('download_close_failed')
        lib.XFlush(display)
    finally:lib.XCloseDisplay(display)


def improve_image(binding,cfg,image,info,deadline,*,adapter=DesktopImages):
    """Return only the original bound cache's bytes; a click is not download success."""
    from wxbridge.ai.images import read_local_image,MediaError
    proof=binding.get('download') or {}
    title=proof.get('chat_title')
    if (cfg.get('ui',{}).get('backend')!='native_service' or proof.get('verified') is not True
            or not isinstance(title,str) or not title.strip() or not proof.get('server_id')):
        return image,{**info,'download':dict(outcome='download_unverified')}
    started=time.monotonic()
    if started>=deadline:
        return image,{**info,'download':dict(outcome='download_timeout',elapsed_ms=0)}
    try:
        with download_lock(cfg,deadline):
            desktop=adapter(cfg,deadline)
            try:
                history=desktop.open_gallery(title);desktop.open_matching(history,image)
                while time.monotonic()<deadline:
                    try:
                        data,updated=read_local_image(binding,cfg)
                        if updated['quality'] in ('original','cached'):
                            return data,{**updated,'download':dict(outcome='downloaded',elapsed_ms=round((time.monotonic()-started)*1000))}
                    except MediaError:pass
                    time.sleep(.2)
                raise DownloadError('download_timeout')
            finally:
                desktop.close()  # Finish UI cleanup before releasing the shared UI lock.
    except Exception as exc:
        code=str(exc) if isinstance(exc,DownloadError) else 'download_ui_unavailable'
        return image,{**info,'download':dict(outcome=code,elapsed_ms=round((time.monotonic()-started)*1000))}
