#!/usr/bin/python

import sys, os, os.path, signal
#from smbus import SMBus
from time import time, sleep, strftime
from fcntl import ioctl
from threading import Thread

defaults = {
	"ttemp" :	0.,
	"thyst" :	0.5,
	"heat" :	"/sys/class/gpio/relay1/value",
	"rel2" :	"/sys/class/gpio/relay2/value",
	"detach" :	0,
	"pidfn" :	"/tmp/main.pid",
	"errfn" :	"/tmp/main.err",
	"logfn" :	None,
	"temp_adt" :	"/sys/bus/i2c/devices/0-0048/temp1_input",
	"temp_htu" :	"/sys/bus/i2c/devices/0-0040/iio:device0/in_temp_input",
	"rh_htu" :	"/sys/bus/i2c/devices/0-0040/iio:device0/in_humidityrelative_input",
	"o2" :		"/sys/bus/i2c/devices/0-0049/in0_input",
	"co2_bus" :	0,
	"co2_addr":	0x15,
	"lcd" :		True,
	"lcdw" :	20,
	"lcdh" :	4,
}

run = 1

class gpio1(object):
	def __init__(self, val):
		self.val = val

	def get(self):
		with open(self.val) as f:
			return int(f.read())

	def set(self, v):
		with open(self.val, "w") as f:
			f.write("1" if v else "0")

def tstat(sv, heat):
	global ttemp, thyst

	t = sv["ta"].val

	if t < 0:
		heat.set(0)
		return

	if t - ttemp < -thyst:
		heat.set(1)
	elif t - ttemp > 0:
		heat.set(0)

class sensval:
	def __init__(self, val, label, units, prec):
		self.val = val
		self.label = label
		self.units = units
		self.prec = prec

	def __str__(self):
		fmt = "%." + str(self.prec) + "f"
		return fmt % self.val

class sensor(object):
	def __init__(self, scale, label, units, prec):
		self.scale = scale
		self.label = label
		self.units = units
		self.prec = prec
		self.rep = 0

	def read(self):
		try:
			v = self.scale * self.raw()
			if self.rep:
				sys.stderr.write(": ".join((hts(), self.label, "OK\n")))
				self.rep = 0
		except Exception as e:
			if not self.rep:
				sys.stderr.write(": ".join((hts(), self.label, str(e))) + "\n")
				self.rep = 1
			v = -1

		return sensval(v, self.label, self.units, self.prec)

class insysfs(sensor):
	def __init__(self, path, scale, label, units, prec):
		self.path = path
		super(insysfs, self).__init__(scale, label, units, prec)

	def raw(self):
		with open(self.path) as f:
			val = int(f.read())
		return val

I2C_SLAVE = 0x0703
class t6700(sensor):
	def __init__(self, bus, addr):
		self.cdev, self.addr = ("/dev/i2c-%d" % bus), addr
		self.label = "CO2"
		self.units = "ppm"
		self.prec = 0
		self.scale = 1.
		super(t6700, self).__init__(self.scale, self.label, self.units, self.prec)

	def raw(self):
		fd = os.open(self.cdev, os.O_RDWR)
		ioctl(fd, I2C_SLAVE, self.addr)
		os.write(fd, "".join(map(chr, (0x04, 0x13, 0x8b, 0x00, 0x01))))
		sleep(0.01)
		r = os.read(fd, 4)
		os.close(fd)
		return ord(r[3]) | (ord(r[2]) << 8)

LCD_ESC = "\033["
class clcd:
	def __init__(self, width, height, cdev="/dev/lcd"):
		self.width = width
		self.height = height
		self.cdev = cdev
		self.init()

	def write(self, s):
		with open(self.cdev, "w") as cd:
			cd.write(s)

	def esc(self, s):
		self.write(LCD_ESC + s)

	def clear(self):
		self.esc("2J")

	def home(self):
		self.esc("H")

	def goto(self, x, y):
		self.esc("Lx%dy%d;" % (x, y))

	def init(self):
		# re-init
		self.esc("LI")

		# no cursor
		self.esc("Lc")

		# backlight on
		self.esc("L+")

		self.clear()
		self.home()

def daemonize(errfn):
	if os.fork() > 0:
		sys.exit(0)
	os.setsid()
	if os.fork() > 0:
		sys.exit(0)
	os.umask(0)
	import resource
	maxfd = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
	if (maxfd == resource.RLIM_INFINITY):
		maxfd = 1024
	for fd in range(0, maxfd):
		try:
			os.close(fd)
		except OSError:
			pass

	if errfn:
		sys.stderr = open(errfn, "w", 0)

def hts():
	return strftime("%Y-%m-%d %H:%M:%S")

def lcd_upd(lcd, sv):
	if not lcd:
		return
	kk = ("ta", "co2", "rh", "o2")
	lcd.home()
	lcd.write(hts() + "\n")
	l = 0
	for k in kk:
		s = "%s %s%s" % (sv[k].label, sv[k], sv[k].units)
		s = "%-10s" % s
		l += len(s)
		if l >= lcd.width:
			s += "\n"
			l = 0
		lcd.write(s)

def log_header(log, kk, sens):
	if not log:
		return
	log.write("# time,s\t")
	log.write("\t".join(map(lambda k: "%s,%s" % (sens[k].label, sens[k].units), kk)))
	log.write("\n")

def sdump(log, kk, sv):
	if not log:
		return
	log.write("%.1f " % time())
	log.write("\t".join(map(lambda k: str(sv[k]), kk)))
	log.write("\n")

def i2c_reset():
	pass

def watchdog():
	global heat, tstamp
	rep = 0
	while run:
		if time() - tstamp > 10:
			heat.set(0)
			if not rep:
				sys.stderr.write(hts() + ": main thread hanged\n")
				rep = 1
				i2c_reset()
		else:
			if rep:
				sys.stderr.write(hts() + ": main thread is running\n")
				rep = 0
		sleep(1)

def main(cfg):
	global ttemp, thyst, heat, tstamp

	lcd = clcd(cfg["lcdw"], cfg["lcdh"]) if cfg["lcd"] else None

	wdt = Thread(target = watchdog)
	wdt.start()

	sens = {
		"ta" : insysfs(cfg["temp_adt"], 1e-3, "Ta", "C", 1),
		"th" : insysfs(cfg["temp_htu"], 1e-3, "Th", "C", 1),
		"rh" : insysfs(cfg["rh_htu"], 1e-3, "RH", "%", 0),
		"o2" : insysfs(cfg["o2"], 1., "O2", "mV", 0),
		"co2" : t6700(cfg["co2_bus"], cfg["co2_addr"]),
	}

	ttemp = cfg["ttemp"]
	thyst = cfg["thyst"]

	logk = sens.keys()
	log = open(cfg["logfn"], "a") if cfg["logfn"] else None
	log_header(log, logk, sens)

	while run:
		tstamp = time()
		sv = dict(map(lambda k: (k, sens[k].read()), sens.keys()))
		tstat(sv, heat)
		lcd_upd(lcd, sv)
		sdump(log, logk, sv)
		sleep(1)
	wdt.join(1)

def parse_cmdline(cfg):
	for arg in sys.argv[1:]:
		r = arg.split("=")
		k = r[0]
		v = 1
		if len(r) >= 2:
			try:
				v = int(r[1], 0)
			except:
				v = r[1]
		cfg[k] = v
	print cfg
	return cfg

def quit(sig, fr):
	global run, heat
	run = 0
	heat.set(0)
	if sig == signal.SIGTERM:
		sys.stderr.write(hts() + ": SIGTERM\n")

if __name__ == "__main__":
	global heat
	cfg = parse_cmdline(defaults)

	heat = gpio1(cfg["heat"])

	if cfg["detach"]:
		daemonize(cfg["errfn"])

	if cfg["pidfn"]:
		with open(cfg["pidfn"], "w") as f:
			f.write("%d" % os.getpid())

	signal.signal(signal.SIGTERM, quit)

	try:
		sys.stderr.write(hts() + ": START\n")
		main(cfg)
	finally:
		quit(0, 0)
		sys.stderr.write(hts() + ": STOP\n")
