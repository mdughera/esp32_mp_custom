import socket
import uasyncio as asyncio
import machine
import errno

class Modbus:
    READ_HOLDING_REGISTERS = 0x03
    WRITE_HOLDING_REGISTER = 0x06
    WRITE_HOLDING_REGISTERS = 0x10
    READ_INPUT_REGISTERS = 0x04   
    UDP = 0
    TCP = 1
    RTU = 2
    CRC_16_TABLE = []
    
    def __init__(self, protocol, ip=None, port=None, timeout = 3, tx=None, rx=None):
        for i in range(256):
            buffer = i << 1
            crc = 0
            for _ in range(8, 0, -1):
                buffer >>= 1
                if (buffer ^ crc) & 0x0001:
                    crc = (crc >> 1) ^ 0xA001

                else:
                    crc >>= 1
            self.CRC_16_TABLE.append(crc)
        if(protocol==self.TCP):
            self.ip = ip
            self.port = port
        elif(protocol==self.UDP):
            self.ip = ip
            self.port = port
            self.addr = None
        elif(protocol==self.RTU):
            self.tx=tx
            self.rx=rx
        else:
            raise ValueError("unknown protocol")
        self.protocol = protocol
        self.timeout = timeout # timeout in seconds

    
    def checksum(self, data: bytearray) -> bytearray:
        crc = 0xFFFF
        for ch in data:
            crc = (crc >> 8) ^ self.CRC_16_TABLE[(crc ^ ch) & 0xFF]
        return(bytearray([crc & 0xFF, (crc >> 8) & 0xFF]))

    def prepare_message(self, slave_id, command, address, payload) -> bytearray:
        msg = int.to_bytes(slave_id, 1, 'big') + bytearray([command]) + int.to_bytes(address, 2, 'big') 
        if(command == self.WRITE_HOLDING_REGISTERS):
            msg += int.to_bytes(len(payload), 2, 'big')     # number of registers to be written
            msg += int.to_bytes(len(payload)*2, 1, 'big')   # byte size of data to be written (seems redundant to me but this is the protocol)
            for i in payload:
                msg += int.to_bytes(i, 2, 'big')           
        else:
            msg += int.to_bytes(payload, 2, 'big') # number of registers to be read or single value to be written
        if(self.protocol == self.TCP):
            l = len(msg)
            msg += self.checksum(msg)
            # add MBAP header only for TCP Modbus
            msg = bytearray([0x00, 0x01, 0x00, 0x00]) +int.to_bytes(l, 2, 'big') + msg
        elif(self.protocol == Modbus.UDP):
            msg += self.checksum(msg)
        elif(self.protocol == Modbus.RTU):
            msg += self.checksum(msg)
        else:
            raise ValueError("unknown protocol")
        return msg
    
    async def exec(self, slave_id, command, address, payload):
        if(self.protocol == Modbus.UDP):
            return(await self._udp_exec(slave_id, command, address, payload))
        elif(self.protocol == Modbus.TCP):
            return(await self._tcp_exec(slave_id, command, address, payload))
        elif(self.protocol == Modbus.RTU):
            return(await self._rtu_exec(slave_id, command, address, payload))
        else:
            return(None)
        
    async def _tcp_exec(self, slave_id, command, address, payload):
        req = self.prepare_message(slave_id, command, address, payload)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.ip, self.port), self.timeout)
            print("Connected to the server")
            writer.write(req)
            print("message written")
            await writer.drain()
            print("wait for response")
            data = await asyncio.wait_for(reader.read(-1), self.timeout)
            print("Received response:", data)
        except OSError as e:
            print("OS Error in _tcp_exec:", e)
            raise
        except asyncio.TimeoutError:
            print("Timeout in _tcp_exec")
            raise
        except Exception as e:
            print("Error in _tcp_exec:", e)
            raise
        finally:
            if 'writer' in locals():
                writer.close()
                await writer.wait_closed()
                print("Socket closed")
        if(data[7] != command):
            raise ValueError(f"Modbus slave error {data[8]}")
        else:
            data=data[9:] #strip response header
            return(data)

    async def _udp_exec(self, slave_id, command, address, payload):
        # Resolve address only if needed or address is not cached
        if self.addr is None:
            try:
                self.addr = socket.getaddrinfo(self.ip, self.port)[0][4]
            except Exception as e:
                print(f"Error resolving address: {e}")
            return None  # Handle failure by returning None or custom error message
            
        # If the socket is not created yet, create it and set it up
        try:
            if not hasattr(self, 'sock') or self.sock is None:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.sock.setblocking(False)
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.sock.connect(self.addr)
        except OSError as e:
            print(f"Error creating or connecting UDP socket: {e}")
            return None  # Socket creation failure, handle separately            
            
        req = self.prepare_message(slave_id, command, address, payload)
        #print("Prepared ", req)
        for i in range(4):
            self.sock.send(req)
            try:
                #print("Wait for UDP recv")
                data = await self.recvfrom(512, self.timeout*1000)
                #print("byte ricevuti ", len(data), " attesi ", int.from_bytes(req[4:6], "big")*2+7)   
                if(req[1] != data[3] or len(data) < int.from_bytes(req[4:6], "big")):
                    print("Errore nella risposta")
                    continue
                data=data[5:] #strip response header
                return(data)
            except asyncio.TimeoutError:
                print("Timeout in Modbus UDP receive")
            except OSError as e:
                print("Error in Modbus UDP receive: {}".format(e))
            except ValueError:
                print("UDP Receive value error")
            await asyncio.sleep_ms(500)
        else:
            print("Modbus UDP receive too many failures")
            return(None)
    
    # poll for UDP reply without blocking the other coroutines
    async def recvfrom(self, length, timeout_ms):
        sleep_ms = 100
        #recv_buffer = bytearray(length)
        for i in range(int(timeout_ms/sleep_ms)):
            #print("recv" , i)
            try:
                recv_buffer = self.sock.recv(length)
                if len(recv_buffer) == 0:
                    print("Connection closed by remote")
                else:
                    # Process the received data
                    return recv_buffer
            except OSError as e:
                if(e.errno == errno.EAGAIN):
                    #print("no data yet")
                    await asyncio.sleep_ms(sleep_ms)  # Add an async sleep to yield control
                else:
                    print("\nrecv_from error: {}".format(e))
                    await asyncio.sleep(1)
        raise asyncio.TimeoutError
    
    async def _rtu_exec(self, slave_id, command, address, payload):
        req = self.prepare_message(slave_id, command, address, payload)
        self.uart = machine.UART(1, baudrate=9600, tx=self.tx, rx=self.rx, bits=8, parity=None, stop=1)
        self.uart.write(req)
        data = await self.receive_data(4)
        #if(req[1] != data[1] or len(data) < int.from_bytes(req[4:6], "big")):
        if(data is None or req[1] != data[1]):
            print("Errore nella risposta")
            raise ValueError("Error in serial data received") 
        data=data[3:] #strip response header
        return data

    async def receive_data(self, timeout):
        data = await asyncio.wait_for(self._read_uart_data(), timeout)
        return data

    async def _read_uart_data(self):
        while True:
            if self.uart.any():
                data = self.uart.read()
                #print("Received:", data)  # Process received data here
                return data
            await asyncio.sleep(0.1)  # Adjust sleep duration as needed
