#!/usr/bin/env python
# NOTES:
# Values of 1 = ON, OPEN
# Values of 0 = OFF, CLOSED
from PID import *
import logging

# - Multithreading
from twisted.internet import reactor

# - Modbus
from pymodbus.server.async import StartTcpServer
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.datastore import ModbusSequentialDataBlock
from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext
from pymodbus.transaction import ModbusRtuFramer, ModbusAsciiFramer

# - World Simulator
import sys, random
import pygame
from pygame.locals import *
from pygame.color import *
import pymunk

# Network
import socket

# Argument parsing
import argparse

import os
import sys
import time

# Override Argument parser to throw error and generate help message
# if undefined args are passed
class MyParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write('error: %s\n' % message)
        self.print_help()
        sys.exit(2)

# Create argparser object to add command line args and help option
parser = MyParser(
	description = 'This Python script starts the SCADA/ICS World Server',
	epilog = '',
	add_help = True)

# Add a "-i" argument to receive a filename
parser.add_argument("-t", action = "store", dest="server_addr",
					help = "Modbus server IP address to listen on")

# Print help if no args are supplied
if len(sys.argv)==1:
	parser.print_help()
	sys.exit(1)

# Split and process arguments into "args"
args = parser.parse_args()

logging.basicConfig()
log = logging.getLogger()
log.setLevel(logging.INFO)

# Display settings
SCREEN_WIDTH = 580
SCREEN_HEIGHT = 460
FPS=70.0

# Port the world will listen on
MODBUS_SERVER_PORT=502

# Amount of oil spilled/processed
oil_spilled_amount = 0
oil_processed_amount = 0
#before PID Valve
oil_flow_out = 0
#after PID Valve
oil_flow_in = 0


# PLC Register values for various control functions
# COMMAND SIGNALS
PLC_FEED_PUMP_COM = 0x01 #regs[0]
PLC_OUTLET_VALVE = 0x03 #regs[2]
PLC_SEP_VALVE = 0x04 #regs[3]
OIL_FLOW_SENS = 0x0D #regs[3]
PLC_WASTE_VALVE = 0x08 #regs[7]

# FEEDBACK SIGNALS
PLC_TANK1_LEVEL = 0x02 #regs[1]
PLC_TANK2_LEVEL = 0x05 #regs[4]
PLC_OIL_SPILL = 0x06 #regs[5]
PLC_OIL_PROCESSED = 0x07 #regs[6]
PLC_OIL_UPPER = 0x09 #regs[8]
CALCULATED_FLOW = 0x0A #regs[9]
PLC_OIL_FLOW_AFTER = 0x0B #regs[10]
PLC_CONTROL_VALVE_POS = 0x0C #regs[11]


### COLLISION TYPES ##
# Collision types are required to be assigned to objects to allow differentiation
# between collisions.
flow_meter_in_collision = 0x1
flow_meter_out_collision = 0xA
PID_valve_collision = 0x2
oil_processed_collision = 0x3
tank1_level_collision = 0x4
tank2_level_collision = 0xB
ball_collision = 0x5
outlet_valve_collision = 0x6
sep_valve_collision = 0x7
waste_valve_collision = 0x8
oil_spill_collision = 0x9

### MODBUS HANDLERS - Begin ###
# Aspire to only use PLCGetTag within the world part of the programme.
# This is to support realistic world - the control should happen from the PLC
# In this case the PLC is the HMI, and the world is the real world with sensors
# and actuators.
def PLCSetTag(addr, value):
    context[0x0].setValues(3, addr, [value])

def PLCGetTag(addr):
    return context[0x0].getValues(3, addr, count=1)[0]
### MODBUS HANDLERS - End ###


### GRAPHICAL FUNCTIONS - Begin ###
# All functions that create some sort of a graphical object are here
def to_pygame(p):
    """Small hack to convert pymunk to pygame coordinates"""
    return int(p.x), int(-p.y+600)

# Add OIL to the world space
# Radius - 2 is the smallest possible. If reduced further, becomes invisible.
# IF you decide to change radius, change the draw_ball function too.
# body._bodycontents.v_limit - changes the reaction factor - the lower is less reactive
def add_ball(space, rad, rmass):
    mass = rmass
    radius = rad
    inertia = pymunk.moment_for_circle(mass, 0, radius, (0,0))
    body = pymunk.Body(mass, inertia)
    body._bodycontents.v_limit = 100
    body._bodycontents.h_limit = 1
    x = random.randint(69, 70)
    body.position = x, 565
    shape = pymunk.Circle(body, radius, (0,0))
    shape.friction = 0.0
    shape.collision_type = ball_collision #liquid
    space.add(body, shape)
    return shape

# Add a ball to the space
# IF you decide to change radius (ABOVE) then change this function too.
def draw_ball(screen, ball, color=THECOLORS['brown']):
    p = int(ball.body.position.x), 600-int(ball.body.position.y)
    pygame.draw.circle(screen, THECOLORS['black'], p, int(ball.radius), int(ball.radius))

# VALVES
# Add the separator vessel release
def sep_valve(space):
    body = pymunk.Body()
    body.position = (327, 218)
    radius = 2
    a = (-15, 0)
    b = (15, 0)
    shape = pymunk.Segment(body, a, b, radius)
    shape.collision_type = sep_valve_collision
    space.add(shape)
    return shape

# Add the separator vessel release
def waste_valve(space):
    body = pymunk.Body()
    body.position = (225, 218)
    radius = 2
    a = (-8, 0)
    b = (9, 0)
    shape = pymunk.Segment(body, a, b, radius)
    shape.collision_type = waste_valve_collision
    space.add(shape)
    return shape

# Outlet valve that lets oil from oil tank to the pipes
def outlet_valve(space):
    body = pymunk.Body()
    body.position = (70, 410)
    # Check these coords and adjust
    a = (-14, 0)
    b = (14, 0)
    radius = 2
    shape = pymunk.Segment(body, a, b, radius)
    shape.collision_type = outlet_valve_collision
    space.add(shape)
    return shape

def PID_valve_shape(space, percentageOpen):
    body = pymunk.Body()
    #rotation centre
    body.position = (182,365+percentageOpen/4)
    radius = 1
    #line size
    a = (0, -12)
    b = (0, 12)
    shape = pymunk.Segment(body, a, b, radius)
    shape.collision_type = PID_valve_collision # PID_valve
    space.add(shape)
    return shape
# SENSORS
# Add the tank level sensors
def tank1_level_sensor(space):
    body = pymunk.Body()
    body.position = (115, 520)
    radius = 3
    a = (0, 0)
    b = (0, 0)
    shape = pymunk.Circle(body, radius, (0, 0))
    shape.collision_type = tank1_level_collision # tank_level
    space.add(shape)
    return shape

def tank2_level_sensor(space):
    body = pymunk.Body()
    body.position = (355, 315)
    radius = 3
    a = (0, 0)
    b = (0, 0)
    shape = pymunk.Circle(body, radius, (0, 0))
    shape.collision_type = tank2_level_collision # tank_level
    space.add(shape)
    return shape

# Sensor at the bottom of the world that detects and counts spills
def oil_spill_sensor(space):
    body = pymunk.Body()
    body.position = (0,100)
    radius = 7
    a = (0, 75)
    b = (137, 75)
    shape = pymunk.Segment(body, a, b, radius)
    shape.collision_type = oil_spill_collision # oil spill sensor
    space.add(shape)
    return shape

#Before PID valve
def flow_meter_in(space):
    body = pymunk.Body()
    body.position = (110,350)
    radius = 7
    a = (0, 0)
    b = (0, 30)
    shape = pymunk.Segment(body, a, b, radius)
    shape.collision_type = flow_meter_in_collision # oil spill sensor
    space.add(shape)
    return shape
#After PID valve
def flow_meter_out(space):
    body = pymunk.Body()
    body.position = (255,350)
    radius = 7
    a = (0, 0)
    b = (0, 30)
    shape = pymunk.Segment(body, a, b, radius)
    shape.collision_type = flow_meter_out_collision # oil spill sensor
    space.add(shape)
    return shape

# Sensor at the bottom of the world that detects and counts spills
def oil_processed_sensor(space):
    body = pymunk.Body()
    body.position = (327, 218)
    radius = 7
    a = (-12, -5)
    b = (12, -5)
    shape = pymunk.Segment(body, a, b, radius)
    shape.collision_type = oil_processed_collision # oil processed sensor
    space.add(shape)
    return shape

# MAIN PUMP
# Feed pump that the oil comes out of
def add_pump(space):
    body = pymunk.Body()
    body.position = (70, 585)
    shape = pymunk.Poly.create_box(body, (15, 20), (0, 0), 0)
    space.add(shape)
    return shape

# PIPING
# Draw the various "pipes" that the oil flows through
# TODO: Get rid of magic numbers and add constants + offsets
def add_oil_unit(space):
    body = pymunk.Body()
    body.position = (300,300)

    #oil storage unit
    l1 = pymunk.Segment(body, (-278, 250), (-278, 145), 1) #left side line
    l2 = pymunk.Segment(body, (-278, 145), (-246, 107), 1)
    l3 = pymunk.Segment(body, (-180, 250), (-180, 145), 1) #right side line
    l4 = pymunk.Segment(body, (-180, 145), (-215, 107), 1)

    #pipe to separator vessel
    l5 = pymunk.Segment(body, (-246, 107), (-246, 53), 1) #left side vertical line
    l6 = pymunk.Segment(body, (-246, 53), (-19, 53), 1) #bottom horizontal line
    l7 = pymunk.Segment(body, (-19, 53), (-19, 33), 1)
    l8 = pymunk.Segment(body, (-215, 107), (-215, 80), 1) #right side vertical line
    l9 = pymunk.Segment(body, (-215, 80), (7, 80), 1) #top horizontal line
    l10 = pymunk.Segment(body, (7, 80), (7, 33), 1)

    #separator vessel
    l11 = pymunk.Segment(body, (-19, 31), (-95, 31), 1) #top left horizontal line
    l12 = pymunk.Segment(body, (-95, 31), (-95, -23), 1) #left side vertical line
    l13 = pymunk.Segment(body, (-95, -23), (-83, -23), 1)
    l14 = pymunk.Segment(body, (-83, -23), (-80, -80), 1) #left waste exit line
    l15 = pymunk.Segment(body, (-68, -80), (-65, -23), 1) #right waste exit line
    l16 = pymunk.Segment(body, (-65, -23), (-45, -23), 1)
    l17 = pymunk.Segment(body, (-45, -23), (-45, -67), 1) #elevation vertical line
    l18 = pymunk.Segment(body, (-45, -67), (13, -67), 1) #left bottom line
    l19 = pymunk.Segment(body, (13, -67), (13, -82), 1) #left side separator exit line
    l20 = pymunk.Segment(body, (43, -82), (43, -67), 1) #right side separator exit line
    l21 = pymunk.Segment(body, (43, -67), (65, -62), 1) #rigt side diagonal line
    l22 = pymunk.Segment(body, (65, -62), (77, 31), 1) #right vertical line
    l23 = pymunk.Segment(body, (77, 31), (7, 31), 1) #top right horizontal line
    l24 = pymunk.Segment(body, (-3, -67), (-3, 10), 3) #center separator line
    l35 = pymunk.Segment(body, (-3, 10), (-65, -23), 1)

    #separator exit pipe
    l25 = pymunk.Segment(body, (43, -85), (43, -113), 1) #right side vertical line
    l26 = pymunk.Segment(body, (43, -113), (580, -113), 1) #top horizontal line
    l27 = pymunk.Segment(body, (13, -85), (13, -140), 1) #left vertical line
    l28 = pymunk.Segment(body, (13, -140), (580, -140), 1) #bottom horizontal line

    #waste water pipe
    l29 = pymunk.Segment(body, (-87, -85), (-87, -112), 1) #left side waste line
    l30 = pymunk.Segment(body, (-60, -85), (-60, -140), 1) #right side waste line
    l31 = pymunk.Segment(body, (-87, -112), (-163, -112), 1) #top horizontal line
    l32 = pymunk.Segment(body, (-60, -140), (-134, -140), 1) #bottom horizontal line
    l33 = pymunk.Segment(body, (-163, -112), (-163, -185), 1) #left side vertical line
    l34 = pymunk.Segment(body, (-134, -140), (-134, -185), 1) #right side vertical line

    space.add(l1, l2, l3, l4, l5, l6, l7, l8, l9, l10, l11, l12, l13, l14, l15,
                l16, l17, l18, l19, l20, l21, l22, l23, l24, l25,
                l26, l27, l28, l29, l30, l31, l32, l33, l34, l35) # 3

    return (l1,l2,l3,l4,l5,l6,l7,l8,l9,l10,l11,l12,l13,l14,l15,l16,l17,
        l18,l19,l20,l21,l22,l23,l24,l25,l26,l27,l28,l29,l30,
        l31,l32,l33,l34,l35)

# DRAWING HELPERS
# Draw a defined polygon
def draw_polygon(bg, shape):
    points = shape.get_vertices()
    fpoints = []
    for p in points:
        fpoints.append(to_pygame(p))
    pygame.draw.polygon(bg, THECOLORS['black'], fpoints)

# Draw a single line to the screen
def draw_line(screen, line, color = None):
    body = line.body
    pv1 = body.position + line.a.rotated(body.angle) # 1
    pv2 = body.position + line.b.rotated(body.angle)
    p1 = to_pygame(pv1) # 2
    p2 = to_pygame(pv2)
    if color is None:
        pygame.draw.lines(screen, THECOLORS["black"], False, [p1,p2])
    else:
        pygame.draw.lines(screen, color, False, [p1,p2])

# Draw lines from an iterable list
def draw_lines(screen, lines):
    for line in lines:
        body = line.body
        pv1 = body.position + line.a.rotated(body.angle) # 1
        pv2 = body.position + line.b.rotated(body.angle)
        p1 = to_pygame(pv1) # 2
        p2 = to_pygame(pv2)
        pygame.draw.lines(screen, THECOLORS["gray"], False, [p1,p2])

### GRAPHICAL FUNCTIONS - End ###

# ### - COLLISION HANDLERS - Begin - ###
# Returning true makes the two objects collide normally just like "walls/pipes"
def no_collision(space, arbiter, *args, **kwargs):
    return True

# Called when level sensor in tank is hit
def tank1_level_reached(space, arbiter, *args, **kwargs):
    log.debug("Level reached")
    PLCSetTag(PLC_TANK1_LEVEL, 1) # Level Sensor Hit, Tank full
    return False

def tank2_level_reached(space, arbiter, *args, **kwargs):
    log.debug("Level reached")
    PLCSetTag(PLC_TANK2_LEVEL, 1) # Level Sensor Hit, Tank full
    return False

def oil_spilled(space, arbiter, *args, **kwargs):
    global oil_spilled_amount
    log.debug("Oil Spilled")
    oil_spilled_amount = oil_spilled_amount + 1
    PLCSetTag(PLC_OIL_SPILL, oil_spilled_amount) # We lost a unit of oil
    return False

def oil_processed(space, arbiter, *args, **kwargs):
    global oil_processed_amount
    log.debug("Oil Processed")
    oil_processed_amount = oil_processed_amount + 1
    if oil_processed_amount >= 65000:
        PLCSetTag(PLC_OIL_PROCESSED, 65000) # We processed a unit of oil
        PLCSetTag(PLC_OIL_UPPER, oil_processed_amount-65000) # We processed a unit of oil
    else:
        PLCSetTag(PLC_OIL_PROCESSED, oil_processed_amount)
        # We processed a unit of oil
    if oil_processed_amount % 600 <= 5:
        #since there is flow, tank level most be low enough
        PLCSetTag(PLC_TANK2_LEVEL, 0)
    return False

def measure_flow_before(space, arbiter, *args, **kwargs):
    global oil_flow_in
    log.debug("Oil Flow Out")
    oil_flow_in = oil_flow_in + 1
    return False

def measure_flow_after(space, arbiter, *args, **kwargs):
    global oil_flow_out
    log.debug("Oil Flow Out")
    oil_flow_out = oil_flow_out + 1
    PLCSetTag(PLC_OIL_FLOW_AFTER, oil_flow_out)
    if oil_flow_out % 100 <= 10:
        #since there is flow, tank level most be low enough
        PLCSetTag(PLC_TANK1_LEVEL, 0)
    return False

# This is on when separation is on
def sep_open(space, arbiter, *args, **kwargs):
    log.debug("Begin separation")
    return False

# This fires when the separator is not processing
def sep_closed(space, arbiter, *args, **kwargs):
    log.debug("Stop separation")
    return True

def outlet_valve_open(space, arbiter, *args, **kwargs):
    log.debug("Outlet valve open")
    return False

def outlet_valve_closed(space, arbiter, *args, **kwargs):
    log.debug("Outlet valve close")
    return True

def waste_valve_open(space, arbiter, *args, **kwargs):
    log.debug("Waste valve open")
    return False

def waste_valve_closed(space, arbiter, *args, **kwargs):
    log.debug("Waste valve close")
    return True

def run_world():
    #INITIALISE PID
    randradius = 1
    rmass = 0.001
    p = PID(0.6,0.3,0) #0.7,0.3,0
    p.SetPoint = 85
    p.setSampleTime(0.01)
    pygame.init()
    screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
    pygame.display.set_caption("Crude Oil Pretreatment Unit")
    clock = pygame.time.Clock()
    running = True

    # Create game space (world) and set gravity to normal
    space = pymunk.Space() #2
    space.gravity = (0.0, -900.0)

    # COLLISION HANDLER ADDED TO OBJECTS AND COLLISIONS
    # When oil collides with tank_level, call tank1_level_reached
    space.add_collision_handler(tank1_level_collision, ball_collision, begin=tank1_level_reached)
    space.add_collision_handler(tank2_level_collision, ball_collision, begin=tank2_level_reached)
    # When oil touches the oil_spill marker, call oil_spilled
    space.add_collision_handler(oil_spill_collision, ball_collision, begin=oil_spilled)
    # When oil touches the oil_process marker, call oil_processed
    space.add_collision_handler(oil_processed_collision, ball_collision, begin=oil_processed)
    # Initial outlet valve condition is turned off
    space.add_collision_handler(outlet_valve_collision, ball_collision, begin=no_collision)
    # PID_valve collision always true
    space.add_collision_handler(PID_valve_collision, ball_collision, begin=outlet_valve_closed)
    # flow meter only measure not collide
    space.add_collision_handler(flow_meter_in_collision, ball_collision, begin=measure_flow_before)
    # flow meter only measure not collide
    space.add_collision_handler(flow_meter_out_collision, ball_collision, begin=measure_flow_after)
    # Initial sep valve condition is turned off
    space.add_collision_handler(sep_valve_collision, ball_collision, begin=no_collision)
    # Initial waste valve condition is turned off
    space.add_collision_handler(waste_valve_collision, ball_collision, begin=no_collision)

    # Add the objects to the game world
    pump = add_pump(space)
    lines = add_oil_unit(space)
    oil_spill = oil_spill_sensor(space)
    flow_meter_incoming = flow_meter_in(space)
    flow_meter_outgoing = flow_meter_out(space)
    oil_process = oil_processed_sensor(space)
    tank_level1 = tank1_level_sensor(space)
    tank_level2 = tank2_level_sensor(space)

    balls = []
    ticks_to_next_ball = 1

    # Set font settings
    fontBig = pygame.font.SysFont(None, 40)
    fontMedium = pygame.font.SysFont(None, 26)
    fontSmall = pygame.font.SysFont(None, 18)
    percentageOpen = 0
    while running:
        p.update( PLCGetTag(CALCULATED_FLOW) )
        percentageOpen = p.output
        if percentageOpen <= 0:
            percentageOpen = 0
        PID_valve = PID_valve_shape(space, percentageOpen)
        outlet = outlet_valve(space)
        sep_valve_obj = sep_valve(space)
        waste_valve_obj = waste_valve(space)
        #percentageOpen += 0.1

        # Advance the game clock
        clock.tick(FPS)
        for event in pygame.event.get():
            if event.type == QUIT:
                running = False
            elif event.type == KEYDOWN and event.key == K_ESCAPE:
                running = False

        # Load the background picture for the pipe images
        bg = pygame.image.load("oil_unit.png")
        # Background color
        screen.fill(THECOLORS["grey"])

        # Oil Tank outlet valve open/closed collision handler
        if PLCGetTag(PLC_OUTLET_VALVE) == 1:
            space.add_collision_handler(outlet_valve_collision, ball_collision, begin=outlet_valve_open)
        elif PLCGetTag(PLC_OUTLET_VALVE) == 0: # Valve is closed
            space.add_collision_handler(outlet_valve_collision, ball_collision, begin=outlet_valve_closed)

        # Separator valve open/closed collision handler
        if PLCGetTag(PLC_SEP_VALVE) == 1:
            space.add_collision_handler(sep_valve_collision, ball_collision, begin=sep_open)
        else:
            space.add_collision_handler(sep_valve_collision, ball_collision, begin=sep_closed)

        # Waste water valve open/closed collision handler
        if PLCGetTag(PLC_WASTE_VALVE) == 1:
            space.add_collision_handler(waste_valve_collision, ball_collision, begin=waste_valve_open)
        else:
            space.add_collision_handler(waste_valve_collision, ball_collision, begin=waste_valve_closed)


        ticks_to_next_ball -= 1
        randradius = random.randint(1,3)
        rmass = random.uniform(0.0001,0.001)

        if ticks_to_next_ball <= 0 and PLCGetTag(PLC_FEED_PUMP_COM) == 1:
            ticks_to_next_ball = 1
            ball_shape = add_ball(space,randradius, rmass)
            balls.append(ball_shape)

        balls_to_remove = []
        for ball in balls:
            if ball.body.position.y < 0 or ball.body.position.x > SCREEN_WIDTH+150:
                balls_to_remove.append(ball)

            draw_ball(bg, ball, randradius)

        for ball in balls_to_remove:
            space.remove(ball, ball.body)
            balls.remove(ball)

        draw_polygon(bg, pump)
        draw_lines(bg, lines)
        draw_ball(bg, tank_level1, THECOLORS['black'])
        draw_ball(bg, tank_level2, THECOLORS['black'])
        draw_line(bg, sep_valve_obj)
        draw_line(bg, outlet)
        draw_line(bg, waste_valve_obj)
        draw_line(bg, oil_spill, THECOLORS['red'])
        draw_line(bg, flow_meter_incoming, THECOLORS['red'])
        draw_line(bg, flow_meter_outgoing, THECOLORS['red'])
        draw_line(bg, PID_valve, THECOLORS['red'])
        draw_line(bg, oil_process, THECOLORS['red'])

        #draw_ball(screen, separator_feed, THECOLORS['red'])
        title = fontMedium.render(str("Crude Oil Pretreatment Unit"), 1, THECOLORS['blue'])
        name = fontBig.render(str("VirtuaPlant"), 1, THECOLORS['gray20'])
        instructions = fontSmall.render(str("(press ESC to quit)"), 1, THECOLORS['gray'])
        feed_pump_label = fontMedium.render(str("Feed Pump"), 1, THECOLORS['blue'])
        oil_storage_label = fontMedium.render(str("Oil Storage Unit"), 1, THECOLORS['blue'])
        separator_label = fontMedium.render(str("Separator Vessel"), 1, THECOLORS['blue'])
        waste_water_label = fontMedium.render(str("Waste Water Treatment Unit"), 1, THECOLORS['blue'])
        tank_sensor = fontSmall.render(str("Tank Level Sensor"), 1, THECOLORS['blue'])
        separator_release = fontSmall.render(str("Separator Vessel Valve"), 1, THECOLORS['blue'])
        waste_sensor = fontSmall.render(str("Waste Water Valve"), 1, THECOLORS['blue'])
        #outlet_sensor = fontSmall.render(str("Outlet Valve"), 1, THECOLORS['blue'])
        outlet_sensor = fontSmall.render(str(percentageOpen), 1, THECOLORS['blue'])

        bg.blit(title, (300, 40))
        bg.blit(name, (347, 10))
        bg.blit(instructions, (SCREEN_WIDTH-115, 0))
        bg.blit(feed_pump_label, (80, 0))
        bg.blit(oil_storage_label, (125, 100))
        bg.blit(separator_label, (385,275))
        screen.blit(waste_water_label, (265, 490))
        bg.blit(tank_sensor, (125, 50))
        bg.blit(outlet_sensor, (90, 195))
        bg.blit(separator_release, (350, 375))
        bg.blit(waste_sensor, (90, 375))
        screen.blit(bg, (0, 0))

        space.step(1/FPS)
        pygame.display.flip()

        #Remove valve object for smoother collisions
        space.remove(PID_valve)
        space.remove(outlet)
        space.remove(sep_valve_obj)
        space.remove(waste_valve_obj)

        PLCSetTag(PLC_CONTROL_VALVE_POS, percentageOpen)

    if reactor.running:
        reactor.callFromThread(reactor.stop)

store = ModbusSlaveContext(
    di = ModbusSequentialDataBlock(0, [0]*100),
    co = ModbusSequentialDataBlock(0, [0]*100),
    hr = ModbusSequentialDataBlock(0, [0]*100),
    ir = ModbusSequentialDataBlock(0, [0]*100))

context = ModbusServerContext(slaves=store, single=True)

# Modbus PLC server information
identity = ModbusDeviceIdentification()
identity.VendorName  = 'Simmons Oil Refining Platform'
identity.ProductCode = 'SORP'
identity.VendorUrl   = 'http://simmons.com/markets/oil-gas/pages/refining-industry.html'
identity.ProductName = 'SORP 3850'
identity.ModelName   = 'Simmons ORP 3850'
identity.MajorMinorRevision = '2.09.01'

def startModbusServer():
    # Run a modbus server on specified address and modbus port (5020)
    StartTcpServer(context, identity=identity, address=(args.server_addr, MODBUS_SERVER_PORT))

def main():
    reactor.callInThread(run_world)
    startModbusServer()

if __name__ == '__main__':
    sys.exit(main())
