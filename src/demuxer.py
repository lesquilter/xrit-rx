"""
demuxer.py
https://github.com/sam210723/xrit-rx
"""

from collections import deque, namedtuple
import colorama
from colorama import Fore, Back, Style
from time import sleep
from threading import Thread
import sys

import ccsds as CCSDS
import products

import paho.mqtt.client as mqtt


class Demuxer:
    """
    Coordinates demultiplexing of CCSDS virtual channels into xRIT files.
    """

    def __init__(self, config, mqtt_config=None):
        """
        Initialises demuxer class
        """

        # Configure instance globals
        self.config = config            # Configuration tuple
        self.rxq = deque()              # Data receive queue
        self.coreReady = False          # Core thread ready state
        self.coreStop = False           # Core thread stop flag
        self.channels = {}              # List of channel handlers
        self.currentVCID = None         # Current Virtual Channel ID
        self.lastImage = None           # Last image output by demuxer
        self.lastXRIT = None            # Last xRIT file output by demuxer
        self.mqtt_config = mqtt_config

        if self.config.downlink == "LRIT":
            self.coreWait = 54          # Core loop delay in ms for LRIT (108.8ms per packet @ 64 kbps)
        elif self.config.downlink == "HRIT":
            self.coreWait = 1           # Core loop delay in ms for HRIT (2.2ms per packet @ 3 Mbps)

        # Start core demuxer thread
        demux_thread = Thread()
        demux_thread.name = "DEMUX CORE"
        demux_thread.run = self.demux_core
        demux_thread.start()

        if (None != mqtt_config):
            self.mqtt_queue = deque()
            mqtt_thread = Thread()
            mqtt_thread.run = self.mqtt_notify
            mqtt_thread.start()

    def mqtt_notify(self):
        """
        Read the mqtt_queue and send mqtt messages
        """

        client = mqtt.Client()
        client.connect(self.mqtt_config.host, self.mqtt_config.port, 60)

        while not self.coreStop:
            try:
                message = self.mqtt_queue.popleft()
            except IndexError:
                message = None

            if message != None:
                client.publish(self.mqtt_config.topic, message)
            else:
                client.loop(1)

        client.disconnect()

    def demux_core(self):
        """
        Distributes VCDUs to channel handlers.
        """
        
        # Indicate core thread has initialised
        self.coreReady = True

        # Thread globals
        lastVCID = None                         # Last VCID seen
        crclut = CCSDS.CP_PDU.CCITT_LUT(None)   # CP_PDU CRC LUT
        
        # Open VCDU dump file
        dumpf = None
        if self.config.dump != None:
            dumpf = open(self.config.dump, 'wb+')

        # Thread loop
        while not self.coreStop:
            # Pull next packet from queue
            packet = self.pull()
            
            # If queue is not empty
            if packet != None:
                # Parse VCDU
                vcdu = CCSDS.VCDU(packet)

                # Set current VCID
                self.currentVCID = vcdu.VCID

                # Dump raw VCDU to file
                if dumpf != None:
                    # Write packet to file if not fill
                    if vcdu.VCID != 63:
                        dumpf.write(packet)
                    else:
                        # Write single fill packet to file (forces VCDU change on playback)
                        if lastVCID != 63:
                            dumpf.write(packet)

                # Check spacecraft is supported
                if vcdu.SC != "GK-2A":
                    if self.config.verbose:
                        print(Fore.WHITE + Back.RED + Style.BRIGHT + "SPACECRAFT \"{}\" NOT SUPPORTED".format(vcdu.SCID))
                    continue

                # Check for VCID change
                if lastVCID != vcdu.VCID:
                    # Notify channel handlers of VCID change
                    for c in self.channels:
                        self.channels[c].notify(vcdu.VCID)
                    
                    # Print VCID info
                    if self.config.verbose: print()
                    vcdu.print_info()
                    if vcdu.VCID in self.config.blacklist:
                        print("  " + Fore.WHITE + Back.RED + Style.BRIGHT + "IGNORING DATA (CHANNEL IS BLACKLISTED)")
                    lastVCID = vcdu.VCID

                # Discard fill packets
                if vcdu.VCID == 63: continue
                
                # Discard VCDUs in blacklisted VCIDs
                if vcdu.VCID in self.config.blacklist: continue

                # Check channel handler for current VCID exists
                try:
                    self.channels[vcdu.VCID]
                except KeyError:
                    # Create new channel handler instance
                    ccfg = namedtuple('ccfg', 'spacecraft downlink verbose dump output images xrit blacklist keys VCID lut')
                    self.channels[vcdu.VCID] = Channel(ccfg(*self.config, vcdu.VCID, crclut), self)
                    if self.config.verbose: print("  " + Fore.GREEN + Style.BRIGHT + "CREATED NEW CHANNEL HANDLER\n")

                # Pass VCDU to appropriate channel handler
                self.channels[vcdu.VCID].data_in(vcdu)
            else:
                # No packet available, sleep thread
                sleep(self.coreWait / 1000)
        
        # Gracefully exit core thread
        if self.coreStop:
            if dumpf != None:
                dumpf.close()
            return

    def push(self, packet):
        """
        Takes in VCDUs for the demuxer to process
        :param packet: 892 byte Virtual Channel Data Unit (VCDU)
        """

        self.rxq.append(packet)

    def pull(self):
        """
        Pull data from receive queue
        """

        try:
            # Return top item
            return self.rxq.popleft()
        except IndexError:
            # Queue empty
            return None

    def complete(self):
        """
        Checks if receive queue is empty
        """

        return len(self.rxq) == 0

    def stop(self):
        """
        Stops the demuxer loop by setting thread stop flag
        """

        self.coreStop = True


class Channel:
    """
    Virtual channel data handler
    """

    def __init__(self, config, parent):
        """
        Initialises virtual channel data handler
        """

        self.config = config        # Configuration tuple
        self.counter = -1           # VCDU continuity counter
        self.cCPPDU = None          # Current CP_PDU object
        self.cTPFile = None         # Current TP_File object
        self.cProduct = None        # Current product object
        self.demuxer = parent       # Demuxer class instance (parent)


    def data_in(self, vcdu):
        """
        Takes in VCDUs for the channel handler to process
        :param packet: Parsed VCDU object
        """

        # Check VCDU continuity counter
        self.continuity(vcdu)

        # Parse M_PDU
        mpdu = CCSDS.M_PDU(vcdu.MPDU)
        
        # If M_PDU contains CP_PDU header
        if mpdu.HEADER:
            # No current TP_File and CP_PDU header is at the start of M_PDU
            if self.cTPFile == None and mpdu.POINTER == 0:
                # Create CP_PDU for new TP_File
                self.cCPPDU = CCSDS.CP_PDU(mpdu.PACKET)
            
            # Continue unfinished TP_File
            else:
                # If M_PDU contains data from previous CP_PDU
                if mpdu.POINTER != 0:
                    # Finish previous CP_PDU
                    preptr = mpdu.PACKET[:mpdu.POINTER]
                else:
                    # No data to append
                    preptr = b''

                try:
                    lenok, crcok = self.cCPPDU.finish(preptr, self.config.lut)
                    if self.config.verbose: self.check_CPPDU(lenok, crcok)

                    # Handle finished CP_PDU
                    self.handle_CPPDU(self.cCPPDU)
                except AttributeError:
                    if self.config.verbose:
                        print("  " + Fore.WHITE + Back.RED + Style.BRIGHT + "NO CP_PDU TO FINISH (DROPPED PACKETS?)")
                
                # Create new CP_PDU
                postptr = mpdu.PACKET[mpdu.POINTER:]
                self.cCPPDU = CCSDS.CP_PDU(postptr)

                # Need more data to parse CP_PDU header
                if not self.cCPPDU.PARSED:
                    return

                # Handle CP_PDUs less than one M_PDU in length
                if 1 < self.cCPPDU.LENGTH < 886 and len(self.cCPPDU.PAYLOAD) > self.cCPPDU.LENGTH:
                    # Remove trailing null bytes (M_PDU padding)
                    self.cCPPDU.PAYLOAD = self.cCPPDU.PAYLOAD[:self.cCPPDU.LENGTH]
                    
                    try:
                        lenok, crcok = self.cCPPDU.finish(b'', self.config.lut)
                        if self.config.verbose: self.check_CPPDU(lenok, crcok)

                        # Handle finished CP_PDU
                        self.handle_CPPDU(self.cCPPDU)
                    except AttributeError:
                        if self.config.verbose:
                            print("  " + Fore.WHITE + Back.RED + Style.BRIGHT + "NO CP_PDU TO FINISH (DROPPED PACKETS?)")

            # Handle special EOF CP_PDU (by ignoring it)
            if self.cCPPDU.is_EOF():
                self.cCPPDU = None
                if self.config.verbose:
                    print("   " + Fore.GREEN + Style.BRIGHT + "[CP_PDU] EOF MARKER\n")
            else:
                if self.config.verbose:
                    self.cCPPDU.print_info()
                    print("    HEADER:     0x{}".format(self.cCPPDU.header.hex().upper()))
                    print("    OFFSET:     0x{}\n    ".format(hex(mpdu.POINTER)[2:].upper()), end="")
        else:
            # Append M_PDU payload to current CP_PDU
            try:
                # Check if CP_PDU header has been parsed
                wasparsed = self.cCPPDU.PARSED

                # Add data from current M_PDU
                self.cCPPDU.append(mpdu.PACKET)

                # If CP_PDU header was just parsed, print CP_PDU header info
                if wasparsed != self.cCPPDU.PARSED and self.config.verbose:
                    self.cCPPDU.print_info()
                    print("    HEADER:     0x{}".format(self.cCPPDU.header.hex().upper()))
                    print("    OFFSET:     SPANS MULTIPLE M_PDUs\n", end="")
            except AttributeError:
                if self.config.verbose:
                    print("  " + Fore.WHITE + Back.RED + Style.BRIGHT + "NO CP_PDU TO APPEND M_PDU TO (DROPPED PACKETS?)")
        
        # VCDU indicator
        if self.config.verbose: print(".", end="")
        sys.stdout.flush()
    

    def continuity(self, vcdu):
        """
        Checks VCDU packet continuity by comparing packet counters
        """

        # If at least one VCDU has been received
        if self.counter != -1:
            # Check counter reset
            if self.counter == 16777215 and vcdu.COUNTER == 0:
                self.counter = vcdu.COUNTER
                return
            
            diff = vcdu.COUNTER - self.counter - 1
            if diff > 0:
                if self.config.verbose:
                    print("  " + Fore.WHITE + Back.RED + Style.BRIGHT + "DROPPED {} PACKET{}    (CURRENT: {}   LAST: {}   VCID: {})".format(diff, "S" if diff > 1 else "", vcdu.COUNTER, self.counter, vcdu.VCID))
                else:
                    print("    " + Fore.WHITE + Back.RED + Style.BRIGHT + "DROPPED {} PACKET{}".format(diff, "S" if diff > 1 else ""))
        
        self.counter = vcdu.COUNTER
    

    def check_CPPDU(self, lenok, crcok):
        """
        Checks length and CRC of finished CP_PDU
        """

        # Show length error
        if lenok:
            print("\n    " + Fore.GREEN + Style.BRIGHT + "LENGTH:     OK")
        else:
            ex = self.cCPPDU.LENGTH
            ac = len(self.cCPPDU.PAYLOAD)
            diff = ac - ex
            print("\n    " + Fore.WHITE + Back.RED + Style.BRIGHT + "LENGTH:     ERROR (EXPECTED: {}, ACTUAL: {}, DIFF: {})".format(ex, ac, diff))

        # Show CRC error
        if crcok:
            print("    " + Fore.GREEN + Style.BRIGHT + "CRC:        OK")
        else:
            print("    " + Fore.WHITE + Back.RED + Style.BRIGHT + "CRC:        ERROR")
        print()


    def handle_CPPDU(self, cppdu):
        """
        Processes complete CP_PDUs to build a TP_File
        """

        if cppdu.SEQ == cppdu.Sequence.FIRST:
            # Create new TP_File
            self.cTPFile = CCSDS.TP_File(cppdu.PAYLOAD[:-2])

        elif cppdu.SEQ == cppdu.Sequence.CONTINUE:
            # Add data to TP_File
            self.cTPFile.append(cppdu.PAYLOAD[:-2])

        elif cppdu.SEQ == cppdu.Sequence.LAST:
            # Close current TP_File
            lenok = self.cTPFile.finish(cppdu.PAYLOAD[:-2])

            if self.config.verbose: self.cTPFile.print_info()
            if lenok:
                if self.config.verbose: print("    " + Fore.GREEN + Style.BRIGHT + "LENGTH:     OK\n")
                
                # Handle S_PDU (decryption)
                spdu = CCSDS.S_PDU(self.cTPFile.PAYLOAD, self.config.keys)

                # Handle xRIT file
                self.handle_xRIT(spdu)

                # Print key index
                if self.config.verbose:
                    print("    KEY INDEX:  0x{}\n".format(hex(int.from_bytes(spdu.index, byteorder="big"))[2:].upper()))

            elif not lenok:
                ex = self.cTPFile.LENGTH
                ac = len(self.cTPFile.PAYLOAD)
                diff = ac - ex

                if self.config.verbose:
                    print("    " + Fore.WHITE + Back.RED + Style.BRIGHT + "LENGTH:     ERROR (EXPECTED: {}, ACTUAL: {}, DIFF: {})".format(ex, ac, diff))
                print("    " + Fore.WHITE + Back.RED + Style.BRIGHT + "SKIPPING FILE DUE TO DROPPED PACKETS")
            
            # Clear finished TP_File
            self.cTPFile = None

        if self.config.verbose:
            ac = len(self.cTPFile.PAYLOAD)
            ex = self.cTPFile.LENGTH
            p = round((ac/ex) * 100)
            diff = ex - ac
            print("    [TP_File]  CURRENT LEN: {} ({}%)     EXPECTED LEN: {}     DIFF: {}\n\n\n".format(ac, p, ex, diff))

    def handle_xRIT(self, spdu):
        """
        Processes complete S_PDUs to build xRIT and Image files
        """

        # Create new xRIT object
        xrit = CCSDS.xRIT(spdu.PLAINTEXT)

        # Save xRIT file if enabled
        if self.config.xrit:
            xrit.save(self.config.output)
            self.demuxer.lastXRIT = xrit.get_save_path(self.config.output)

        # Save image file if enabled
        if self.config.images:
            # Create new product
            if self.cProduct == None:
                self.cProduct = products.new(self.config, xrit.FILE_NAME)
                self.cProduct.print_info()
            
            # Add data to current product
            self.cProduct.add(xrit)

            # Save and clear complete product
            if self.cProduct.complete:
                self.cProduct.save()
                self.demuxer.lastImage = self.cProduct.last

                # send mqtt notification
                path = self.cProduct.get_save_path(self.cProduct.get_ext())
                path = "/".join(path.split("/")[-4:])
                self.demuxer.mqtt_queue.append(path)

                self.cProduct = None
        else:
            # Print XRIT file info
            xrit.print_info(self.config.verbose)


    def notify(self, vcid):
        """
        Notifies virtual channel handler of change in VCID
        """

        # No longer the active channel handler  
        if vcid != self.config.VCID:
            # Channel has unfinished TP_File
            if self.cTPFile != None:
                # Handle S_PDU (decryption)
                spdu = CCSDS.S_PDU(self.cTPFile.PAYLOAD, self.config.keys)

                # Handle xRIT file
                self.handle_xRIT(spdu)

                if len(self.cTPFile.PAYLOAD) < self.cTPFile.LENGTH:
                    print("    " + Fore.WHITE + Back.RED + Style.BRIGHT + "FILE IS INCOMPLETE")
                    ac = len(self.cTPFile.PAYLOAD)
                    ex = self.cTPFile.LENGTH
                    p = round((ac/ex) * 100)
                    print("    " + Fore.WHITE + Back.RED + Style.BRIGHT + "{}% OF EXPECTED LENGTH".format(p))

                # Clear finished TP_File
                self.cTPFile = None
            elif self.cProduct != None:
                # Save and clear current product
                self.cProduct.save()
                self.cProduct = None
