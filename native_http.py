"""Windows HTTPS via WinHTTP/SChannel. No certificate exceptions or credential access."""
import ctypes
from ctypes import wintypes as w
import urllib.parse

class WindowsDownloadError(Exception):
    def __init__(self, code, stage, flags=0, status=None):
        self.code=code;self.stage=stage;self.flags=flags;self.status=status
        reasons=[]
        for bit,text in [(1,'не удалось проверить отзыв сертификата'),(2,'некорректный сертификат'),(4,'сертификат отозван'),(8,'недоверенный центр сертификации / цепочка'),(16,'имя сервера не совпадает'),(32,'срок сертификата не подходит'),(64,'неверное назначение сертификата'),(0x80000000,'сбой защищённого канала')]:
            if flags & bit:reasons.append(text)
        if not reasons:
            reasons=[{12002:'истёк тайм-аут сети',12007:'не удалось разрешить имя сервера',12029:'соединение не установлено',12030:'соединение прервано',12037:'срок сертификата не подходит',12038:'имя сертификата не совпадает',12045:'недоверенный центр сертификации / цепочка',12175:'Windows отклонила TLS-сертификат или защищённый канал'}.get(code,'ошибка системного HTTPS')]
        self.reason='; '.join(reasons) if status is None else f'HTTP {status}'
        super().__init__(self.reason)

def stream(url, output, maximum):
    """Write a pinned HTTPS response. Caller validates allowlist and SHA256."""
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None,443):
        raise ValueError('HTTPS URL without credentials/query required')
    dll=ctypes.WinDLL('winhttp.dll',use_last_error=True,winmode=0x00000800)  # System32 only
    handle=w.HANDLE;dword=w.DWORD;ptr=ctypes.c_void_p;pdword=ctypes.POINTER(dword)
    signatures={
      'WinHttpOpen':([w.LPCWSTR,dword,w.LPCWSTR,w.LPCWSTR,dword],handle),
      'WinHttpConnect':([handle,w.LPCWSTR,w.WORD,dword],handle),
      'WinHttpOpenRequest':([handle,w.LPCWSTR,w.LPCWSTR,w.LPCWSTR,w.LPCWSTR,ptr,dword],handle),
      'WinHttpSetTimeouts':([handle,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int],w.BOOL),
      'WinHttpSetOption':([handle,dword,ptr,dword],w.BOOL),
      'WinHttpSetStatusCallback':([handle,ptr,dword,ctypes.c_size_t],ptr),
      'WinHttpSendRequest':([handle,w.LPCWSTR,dword,ptr,dword,dword,ctypes.c_size_t],w.BOOL),
      'WinHttpReceiveResponse':([handle,ptr],w.BOOL),
      'WinHttpQueryHeaders':([handle,dword,w.LPCWSTR,ptr,pdword,pdword],w.BOOL),
      'WinHttpReadData':([handle,ptr,dword,pdword],w.BOOL),
      'WinHttpCloseHandle':([handle],w.BOOL)}
    for name,(args,result) in signatures.items():
        func=getattr(dll,name);func.argtypes=args;func.restype=result
    handles=[];tls_flags=[0]
    callback_type=ctypes.WINFUNCTYPE(None,handle,ctypes.c_size_t,dword,ptr,dword)
    def status_callback(h,context,status,info,length):
        if status==0x10000 and info and length>=ctypes.sizeof(dword):
            tls_flags[0]|=ctypes.cast(info,pdword).contents.value
    callback=callback_type(status_callback)
    def checked(result,stage):
        if not result:raise WindowsDownloadError(ctypes.get_last_error(),stage,tls_flags[0])
        return result
    def option(request,number,value):
        data=dword(value)
        checked(dll.WinHttpSetOption(request,number,ctypes.byref(data),ctypes.sizeof(data)),'настройка HTTPS')
    try:
        session=checked(dll.WinHttpOpen('MinecraftModpackUpdater/r6',4,None,None,0),'открытие HTTPS');handles.append(session)
        checked(dll.WinHttpSetTimeouts(session,10000,30000,30000,45000),'тайм-ауты')
        connection=checked(dll.WinHttpConnect(session,parsed.hostname,443,0),'подключение');handles.append(connection)
        request=checked(dll.WinHttpOpenRequest(connection,'GET',parsed.path,None,None,None,0x00800000),'HTTPS-запрос');handles.append(request)
        # Disable cookies (1), redirects (2), automatic authentication (4).
        # Never use WINHTTP_OPTION_SECURITY_FLAGS or certificate-ignore flags.
        option(request,63,1|2|4)
        # Enable certificate revocation checks in addition to normal Windows
        # chain, date, name and purpose validation. Never ignore offline errors.
        option(request,79,1)
        previous=dll.WinHttpSetStatusCallback(request,ctypes.cast(callback,ptr),0x10000,0)
        if previous==ctypes.c_void_p(-1).value:raise WindowsDownloadError(ctypes.get_last_error(),'диагностика TLS')
        checked(dll.WinHttpSendRequest(request,None,0,None,0,0,0),'отправка запроса')
        checked(dll.WinHttpReceiveResponse(request,None),'проверка TLS / ответ')
        status=dword();size=dword(ctypes.sizeof(status))
        checked(dll.WinHttpQueryHeaders(request,19|0x20000000,None,ctypes.byref(status),ctypes.byref(size),None),'статус HTTP')
        if status.value!=200:raise WindowsDownloadError(0,'ответ HTTP',status=status.value)
        total=0;buffer=ctypes.create_string_buffer(64*1024);received=dword()
        while True:
            checked(dll.WinHttpReadData(request,buffer,len(buffer),ctypes.byref(received)),'получение файла')
            if not received.value:break
            total+=received.value
            if total>maximum:raise ValueError('Размер скачанного файла больше ожидаемого')
            output.write(buffer.raw[:received.value])
        return total
    finally:
        for h in reversed(handles):dll.WinHttpCloseHandle(h)
