 #To podobno jest driver do Apolla. WRITTEN BY PomPa.  <Kontakt@napompuj.SE>
#PORT="rfc2217://helpme.dziwisz.net:9999"
PORT='com3'
import threading
import time
from . import numbers_pl
import queue
import datetime
import synthDriverHandler
from synthDriverHandler import SynthDriver, VoiceInfo, synthIndexReached, synthDoneSpeaking
import speech
from speech.commands import IndexCommand
from . import cserial
#from .cserial import rfc2217
from .cserial import rs485
import logging
import driverHandler
indexLookAheadOffset = 0
lastindex = 0
indexPollingInterval = 0.10
serialLock = threading.Lock()
#indexFile = open('C:\\index.txt','wb')
minRate = 1
maxRate = 9
minPitch = 0
maxPitch = 15
minVolume=0
maxVolume=15
minInflection = 0
maxInflection = 7
minVoicing = 1
maxVoicing = 8
minSentencePause = 0
maxSentencePause=15
minWordPause = 0
maxWordPause=9
def getTime():
 return str(datetime.datetime.now().time())

class IndexPollingThread(threading.Thread):
 def __init__(self):
  threading.Thread.__init__(self)
  self.keepRunning = True

 def run(self):
  while(self.keepRunning):
   _bgExec(serialQueue, bgWrite, b'@I?')
   time.sleep(indexPollingInterval)

#This can't terminate...
class IndexingThread(threading.Thread):
 def __init__(self, onIndexReached):
  threading.Thread.__init__(self)
  self.previousIndex = 0
  self.onIndexReached = onIndexReached
  self.keepRunning = True
 def getLastIndex(self):
  command = port.read(1)
  if command!=b'I':
   return None
  indexCommandResult = port.read(3)
  index = int(indexCommandResult[1::-1], 16)
#  indexFile.write(bytes(getTime() + ';'+ 'received index ' + str(index), encoding='utf8')+b'\r\n')
  return index

 def run(self):
  while self.keepRunning:
   index = self.getLastIndex()
   if index is None:
    continue
   while (index - indexLookAheadOffset) < indexQueue.qsize() and indexQueue.qsize() > 0:
    self.onIndexReached(indexQueue.get())
    indexQueue.task_done()

   if indexQueue.qsize() == 0:
    self.onIndexReached(None)

def clear_queue(q):
 try:
  while True:
   q.get_nowait()
 except:
  pass

port = None
serialQueue = queue.Queue()
indexQueue = queue.Queue()
class BgThread(threading.Thread):
 def __init__(self, q):
  threading.Thread.__init__(self)
  self.setDaemon(True)
  self.q = q

 def run(self):
  try:
   while True:
    func, args, kwargs = self.q.get()
    if not func:
     break
    func(*args, **kwargs)
    self.q.task_done()
  except:
   logging.error("bgThread.run", exc_info=True)

def _bgExec(q, func, *args, **kwargs):
 q.put((func, args, kwargs))

def bgWrite(text):
 #timeout errors. Ignore all errors for now, fix later to ignore the specific error
 try:
#  serialLock.acquire()
#  port.rts = True
  port.write(text)
#  indexFile.write(bytes(getTime() + ';Sending text: ', encoding='utf8'))
#  indexFile.write(text+b'\r\n')
 except serial.SerialTimeoutException:
  logging.error("bgWrite", exc_info=True)
# finally:
#  port.rts = False
#  serialLock.release()

class SynthDriver(SynthDriver):
 name = "apollo"
 description = "test apollo 2"
 supportedSettings = (SynthDriver.RateSetting(10),SynthDriver.PitchSetting(5),SynthDriver.VolumeSetting(10),SynthDriver.InflectionSetting(10), synthDriverHandler.NumericDriverSetting("sentencePause", _("&Sentence pause"), minStep=10), synthDriverHandler.NumericDriverSetting("wordPause", _("&Word pause"), minStep=10), synthDriverHandler.NumericDriverSetting("voicing", _("&Voicing"), minStep=10))
# supportedCommants = {IndexCommand}
 supportedNotifications = {synthIndexReached, synthDoneSpeaking}
 @classmethod
 def check(cls):
  return True

 def __init__(self):
  global port
  self.lastChecked = time.time()
#  port = rfc2217.Serial(PORT, 9600)
  port = cserial.serial_for_url(PORT, 9600)
  port.dsrdtr = False
  port.rs485_mode = rs485.RS485Settings()
  self.serialThread = BgThread(serialQueue)
  self.serialThread.start()
  self.indexingThread = IndexingThread(self.onIndexReached)
  self.indexingThread.start()
  self.indexPollingThread = IndexPollingThread()
  self.indexPollingThread.start()
  self.dt_rate = 2
  self.dt_pitch = 11
  self.dt_volume = 9
  self.inflection = 3
  self.dt_voicing = 8
  self.dt_sentencePause = 11
  self.dt_wordPause = 0

 def speak(self, speechSequence):
  text_list = []
  for item in speechSequence:
   if isinstance(item, str):
    item.replace('@',' ')

    text_list.append(item)
   elif isinstance(item,IndexCommand):
   # text_list.append("\x00%di" % item.index)
    text_list.append("@I+")
#    indexFile.write(bytes(getTime() + ';Sending index ' + str(item.index), encoding='utf8')+b'\r\n')
    indexQueue.put(item.index)

  #No unicode here. Do something better with this later
  text = u" ".join(text_list)
#  text = text.replace('\r', ' ')
#  text = text.replace('\n', ' ')
  text = numbers_pl.dajNapisZLiczbamiWPostaciSlownej(text)
  text = text.encode('1250', 'replace')
  text = text.replace(b'\xb9', b'\x86')#ą
  text = text.replace(b'\xE6', b'\x8D')#ć
  text = text.replace(b'\xEA', b'\x91')#ę
  text = text.replace(b'\xB3', b'\x92')#ł
  text = text.replace(b'\xF1', b'\xA4')#ń
  text = text.replace(b'\xF3', b'\xA2')#ó
  text = text.replace(b'\x9C', b'\x9E')#ś
  text = text.replace(b'\x9F', b'\xA6')#ź
  text = text.replace(b'\xBF', b'\xA7')#ż
  text = text.replace(b'\xC6', b'\x95')#Ć
  text = text.replace(b'\xCA', b'\x90')#Ę
  text = text.replace(b'\xA3', b'\x9C')#Ł
  text = text.replace(b'\xD3', b'\xA3')#Ó
  text = text.replace(b'\x8C', b'\x98')#Ś
  text = text.replace(b'\x8F', b'\xA0')#Ź
  text = text.replace(b'\xAF', b'\xA1')#Ż
  text = text.replace(b'\xA5', b'\x8F')#Ą
  text = text.replace(b'\xD1', b'\xA5')#Ń
  if self.dt_pitch<=9:
   my_dt_pitch=str(self.dt_pitch)
  if self.dt_pitch==10:
   my_dt_pitch="A"
  if self.dt_pitch==11:
   my_dt_pitch="B"
  if self.dt_pitch==12:
   my_dt_pitch="C"
  if self.dt_pitch==13:
   my_dt_pitch="D"
  if self.dt_pitch==14:
   my_dt_pitch="E"
  if self.dt_pitch==15:
   my_dt_pitch="F"
  if self.dt_volume<=9:
   my_dt_volume=str(self.dt_volume)
  if self.dt_volume==10:
   my_dt_volume="A"
  if self.dt_volume==11:
   my_dt_volume="B"
  if self.dt_volume==12:
   my_dt_volume="C"
  if self.dt_volume==13:
   my_dt_volume="D"
  if self.dt_volume==14:
   my_dt_volume="E"
  if self.dt_volume==15:
   my_dt_volume="F"
  text = b"@w%s @f%s @a%s @r%s @b%s @d%s @q%s %b" % (bytes(hex(self.dt_rate)[2:], encoding='utf8'), bytes(hex(self.dt_pitch)[2:], encoding='utf8'), bytes(hex(self.dt_volume)[2:], encoding='utf8'), bytes(hex(self.dt_inflection)[2:], encoding='utf8'), bytes(str(self.dt_voicing), encoding='utf8'), bytes(hex(self.dt_sentencePause)[2:], encoding='utf8'), bytes(hex(self.dt_wordPause)[2:], encoding='utf8'), text)
  if text:
   _bgExec(serialQueue, bgWrite, text + b'\r')

 def cancel(self):
  global lastindex
#  indexFile.write(bytes(getTime() + ';cancel\r\n', encoding='utf8'))
  clear_queue(serialQueue)
  clear_queue(indexQueue)
  lastindex = 0
  self.indexingThread.previousIndex = 0
  
  _bgExec(serialQueue, bgWrite, b'@t5 \x18 @I+')
  self.onIndexReached(None)
#  try:
#   port.write('@t5 \x18')
#   port.write("@i? @i+")
#   port.write(str(lastindex))
#  except serial.SerialTimeoutException:
#   pass

 def _set_rate(self, vl):
  self.dt_rate = self._percentToParam(vl,minRate,maxRate)

 def _get_rate(self):
  return self._paramToPercent(self.dt_rate, minRate, maxRate)

 def _set_pitch(self, vl):
  self.dt_pitch = self._percentToParam(vl, minPitch, maxPitch)

 def _get_pitch(self):
  return self._paramToPercent(self.dt_pitch, minPitch, maxPitch)

 def _set_volume(self, vl):
  self.dt_volume = self._percentToParam(vl,minVolume,maxVolume)

 def _get_volume(self):
  return self._paramToPercent(self.dt_volume, minVolume, maxVolume)

 def _set_inflection(self, vl):
  self.dt_inflection = self._percentToParam(vl,minInflection,maxInflection)

 def _get_inflection(self):
  return self._paramToPercent(self.dt_inflection, minInflection, maxInflection)

 def _set_wordPause(self, vl):
  self.dt_wordPause = self._percentToParam(vl,minWordPause,maxWordPause)

 def _get_wordPause(self):
  return self._paramToPercent(self.dt_wordPause, minWordPause, maxWordPause)

 def _set_sentencePause(self, vl):
  self.dt_sentencePause = self._percentToParam(vl,minSentencePause,maxSentencePause)

 def _get_sentencePause(self):
  return self._paramToPercent(self.dt_sentencePause, minSentencePause, maxSentencePause)

 def _set_voicing(self, vl):
  self.dt_voicing = self._percentToParam(vl,minVoicing,maxVoicing)

 def _get_voicing(self):
  return self._paramToPercent(self.dt_voicing, minVoicing, maxVoicing)

 def terminate(self):
#  indexFile.write(bytes(getTime() + ';terminating\r\n', encoding='utf8'))
  clear_queue(serialQueue)
  clear_queue(indexQueue)
  serialQueue.put((None, None, None))
  self.indexingThread.keepRunning = False
  self.indexPollingThread.keepRunning = False

  port.close()
# def _get_lastIndex(self):

#  global lastindex
#  if time.time()-self.lastChecked>=0.5:
#   _bgExec(serialQueue, bgWrite, '@I?')

#   self.lastChecked = time.time()
#  return(lastindex)
 def onIndexReached(self, index):
  if index is not None:
   synthIndexReached.notify(synth=self, index=index)
#   indexFile.write(bytes(getTime() + ';reached index ' + str(index), encoding='utf8')+b'\r\n')
  else:
   synthDoneSpeaking.notify(synth=self)
#   indexFile.write(bytes(getTime() + ';done speaking\r\n', encoding='utf8'))