"""Split only the distribution ZIP; the EXE and modpack manifest stay unchanged."""
import hashlib
import json
from pathlib import Path

FILENAME='Minecraft-Modpack-Updater-Windows.zip'

def split(downloads):
    downloads=Path(downloads)
    data=(downloads/FILENAME).read_bytes()
    cut=(len(data)+1)//2
    parts=[]
    for i,chunk in enumerate((data[:cut],data[cut:]),1):
        name=FILENAME+'.part'+str(i)
        assert 0<len(chunk)<8_000_000
        (downloads/name).write_bytes(chunk)
        parts.append({'file':name,'bytes':len(chunk),'sha256':hashlib.sha256(chunk).hexdigest()})
    spec={'schema':1,'file':FILENAME,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'parts':parts}
    (downloads/'windows-download.json').write_text(json.dumps(spec,indent=2)+'\n',encoding='utf-8')
    return spec

if __name__=='__main__':
    print(json.dumps(split(Path(__file__).parent/'docs/downloads'),indent=2))
