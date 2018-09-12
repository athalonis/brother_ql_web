#!/usr/bin/env python3

"""
This is a web service to print labels on Brother QL label printers.
"""

import sys, logging, random, json, argparse, os, io
from io import BytesIO

from bottle import run, route, get, post, response, request, jinja2_view as view, static_file, redirect
from PIL import Image, ImageDraw, ImageFont

from brother_ql.devicedependent import models, label_type_specs, label_sizes
from brother_ql.devicedependent import ENDLESS_LABEL, DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL
from brother_ql import BrotherQLRaster, create_label
from brother_ql.backends import backend_factory, guess_backend

from font_helpers import get_fonts

logger = logging.getLogger(__name__)

LABEL_SIZES = [ (name, label_type_specs[name]['name']) for name in label_sizes]

DEBUG = True

try:
    with open('config.json') as fh:
        CONFIG = json.load(fh)
except FileNotFoundError as e:
    with open('config.example.json') as fh:
        CONFIG = json.load(fh)


@route('/')
def index():
    redirect('/labeldesigner')

@route('/static/<filename:path>')
def serve_static(filename):
    return static_file(filename, root='./static')

@route('/labeldesigner')
@view('labeldesigner.jinja2')
def labeldesigner():
    font_family_names = sorted(list(FONTS.keys()))
    return {'font_family_names': font_family_names,
            'fonts': FONTS,
            'label_sizes': LABEL_SIZES,
            'website': CONFIG['WEBSITE'],
            'label': CONFIG['LABEL']}

def get_label_context(request):
    """ might raise LookupError() """

    d = request.params.decode() # UTF-8 decoded form data
    font_family = d.get('font_family', None)
    font_style  = d.get('font_family', None)
    if font_family is not None:
        font_family = font_family.rpartition('(')[0].strip()
    if font_style is not None:
        font_style  = font_style.rpartition('(')[2].rstrip(')')
    context = {
      'text':          d.get('text', None),
      'font_size': int(d.get('font_size', CONFIG['LABEL']['DEFAULT_FONT_SIZE'])),
      'font_family':   font_family,
      'font_style':    font_style,
      'label_size':    d.get('label_size', CONFIG['LABEL']['DEFAULT_SIZE']),
      'kind':          label_type_specs[d.get('label_size', CONFIG['LABEL']['DEFAULT_SIZE'])]['kind'],
      'margin':    int(d.get('margin', 10)),
      'threshold': int(d.get('threshold', 70)),
      'align':         d.get('align', 'center'),
      'orientation':   d.get('orientation', 'standard'),
      'color':         d.get('color', 'black'),
      'margin_top':    float(d.get('margin_top',    24))/100.,
      'margin_bottom': float(d.get('margin_bottom', 45))/100.,
      'margin_left':   float(d.get('margin_left',   35))/100.,
      'margin_right':  float(d.get('margin_right',  35))/100.,
      'qr':d.get('qr', None),
      'qrsize':int(d.get('qrsize', '100')),
    }
    context['margin_top']    = int(context['font_size']*context['margin_top'])
    context['margin_bottom'] = int(context['font_size']*context['margin_bottom'])
    context['margin_left']   = int(context['font_size']*context['margin_left'])
    context['margin_right']  = int(context['font_size']*context['margin_right'])

    def get_font_path(font_family_name, font_style_name):
        try:
            if font_family_name is None or font_style_name is None:
                font_family_name = CONFIG['LABEL']['DEFAULT_FONTS']['family']
                font_style_name =  CONFIG['LABEL']['DEFAULT_FONTS']['style']
            font_path = FONTS[font_family_name][font_style_name]
        except KeyError:
            raise LookupError("Couln't find the font & style")
        return font_path

    context['font_path'] = get_font_path(context['font_family'], context['font_style'])

    def get_label_dimensions(label_size):
        try:
            ls = label_type_specs[context['label_size']]
        except KeyError:
            raise LookupError("Unknown label_size")
        return ls['dots_printable']

    width, height = get_label_dimensions(context['label_size'])
    if height > width: width, height = height, width
    if context['orientation'] == 'rotated': height, width = width, height
    context['width'], context['height'] = width, height

    return context

def create_label_im(text, **kwargs):
    label_type = kwargs['kind']
    im_font = ImageFont.truetype(kwargs['font_path'], kwargs['font_size'])
    im = Image.new('RGB', (20, 20), 'white')
    draw = ImageDraw.Draw(im)
    # workaround for a bug in multiline_textsize()
    # when there are empty lines in the text:
    lines = []
    for line in text.split('\n'):
        if line == '': line = ' '
        lines.append(line)
    text = '\n'.join(lines)
    linesize = im_font.getsize(text)
    textsize = draw.multiline_textsize(text, font=im_font)
    width, height = kwargs['width'], kwargs['height']
    if kwargs['orientation'] == 'standard':
        if label_type in (ENDLESS_LABEL,):
            height = textsize[1] + kwargs['margin_top'] + kwargs['margin_bottom']
    elif kwargs['orientation'] == 'rotated':
        if label_type in (ENDLESS_LABEL,):
            width = textsize[0] + kwargs['margin_left'] + kwargs['margin_right']
    im = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(im)
    if kwargs['orientation'] == 'standard':
        if label_type in (DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL):
            vertical_offset  = (height - textsize[1])//2
            vertical_offset += (kwargs['margin_top'] - kwargs['margin_bottom'])//2
        else:
            vertical_offset = kwargs['margin_top']
        horizontal_offset = max((width - textsize[0])//2, 0)
    elif kwargs['orientation'] == 'rotated':
        vertical_offset  = (height - textsize[1])//2
        vertical_offset += (kwargs['margin_top'] - kwargs['margin_bottom'])//2
        if label_type in (DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL):
            horizontal_offset = max((width - textsize[0])//2, 0)
        else:
            horizontal_offset = kwargs['margin_left']
    offset = horizontal_offset, vertical_offset
    fill = 'black'
    if kwargs['color'] == 'redandblack':
        fill = 'red'

    draw.multiline_text(offset, text, fill, font=im_font, align=kwargs['align'])
    return im

@get('/api/preview/qr')
@post('/api/preview/qr')
def get_preview_image():
    context = get_label_context(request)
    im = create_label_im(**context)
    return_format = request.query.get('return_format', 'png')
    if return_format == 'json':
        response.set_header('Content-type', 'text/json')
        return '{"status":"1"}'
    return '{"status":0}'

@get('/api/preview/text')
@post('/api/preview/text')
def get_preview_image():
    context = get_label_context(request)
    im = create_label_im(**context)
    return_format = request.query.get('return_format', 'png')
    if return_format == 'base64':
        import base64
        response.set_header('Content-type', 'text/plain')
        return base64.b64encode(image_to_png_bytes(im))
    else:
        response.set_header('Content-type', 'image/png')
        return image_to_png_bytes(im)

def image_to_png_bytes(im):
    image_buffer = BytesIO()
    im.save(image_buffer, format="PNG")
    image_buffer.seek(0)
    return image_buffer.read()

@post('/api/print/qrcode')
@get('/api/print/qrcode')
def print_qrcode():
    return_dict = {'success':False}

    try:
        context = get_label_context(request)
        logger.warning(context)
    except LookupError as e:
        return_dict['error'] = e.msg
        return return_dict

    import requests
    from PIL import Image, ImageOps
    from io import BytesIO

    r = requests.get('https://api.qrserver.com/v1/create-qr-code/', 
        params={'data':context['qr'],'size':'%sx%s' % (context['qrsize'],context['qrsize'])})
    logger.warning('REQUEST URL: %s' % (r.url))
    logger.warning(r)
    logger.warning('contents:')
    logger.warning(r.content)
    im = Image.open(BytesIO(r.content))

    im = im.convert('RGB')
    im.save('last-qrcode.png')

    i2 = ImageOps.expand(im, border=300, fill='#ffffff')

    im = i2.crop((0,300, 696,300+context['qrsize']))

    context['width'] = 696
    context['height'] = context['qrsize']

    if context['kind'] == ENDLESS_LABEL:
        rotate = 0 if context['orientation'] == 'standard' else 90
    elif context['kind'] in (ROUND_DIE_CUT_LABEL, DIE_CUT_LABEL):
        rotate = 'auto'

    qlr = BrotherQLRaster(CONFIG['PRINTER']['MODEL'])
    create_label(qlr, im, context['label_size'], threshold=context['threshold'], cut=True, rotate=rotate)

    if not DEBUG:
        try:
            be = BACKEND_CLASS(CONFIG['PRINTER']['PRINTER'])
            be.write(qlr.data)
            be.dispose()
            del be
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG: return_dict['data'] = str(qlr.data)
    return return_dict

@post('/api/print/qrcode2')
@get('/api/print/qrcode2')
def print_qrcode2():
    return_dict = {'success':False}

    try:
        context = get_label_context(request)
        logger.warning(context)
    except LookupError as e:
        return_dict['error'] = e.msg
        return return_dict

    import requests
    from PIL import Image, ImageOps
    from io import BytesIO

    r = requests.get('https://api.qrserver.com/v1/create-qr-code/', 
        params={'data':context['qr'],'size':'%sx%s' % (context['qrsize'],context['qrsize'])})
    logger.warning('REQUEST URL: %s' % (r.url))
    logger.warning(r)
    logger.warning('contents:')
    logger.warning(r.content)
    im = Image.open(BytesIO(r.content))

    im = im.convert('RGB')
    im.save('last-qrcode.png')

    i2 = Image.new('RGB', (696, context['qrsize']), '#ffffff')
#    i2 = ImageOps.expand(im, border=300, fill='#ffffff')
#    im = i2.crop((0,300, 696,300+context['qrsize']))

    # create multiple qrcodes on the label ... 
    xoffset = 0
    while (xoffset < 696):
        i2.paste(im, (xoffset, 0))
        xoffset += context['qrsize'] + 40

    context['width'] = 696
    context['height'] = context['qrsize']

    if context['kind'] == ENDLESS_LABEL:
        rotate = 0 if context['orientation'] == 'standard' else 90
    elif context['kind'] in (ROUND_DIE_CUT_LABEL, DIE_CUT_LABEL):
        rotate = 'auto'

    qlr = BrotherQLRaster(CONFIG['PRINTER']['MODEL'])
    create_label(qlr, i2, context['label_size'], threshold=context['threshold'], cut=True, rotate=rotate)

    if not DEBUG:
        try:
            be = BACKEND_CLASS(CONFIG['PRINTER']['PRINTER'])
            be.write(qlr.data)
            be.dispose()
            del be
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG: return_dict['data'] = str(qlr.data)
    return return_dict


@post('/api/print/qrcodetracker')
@get('/api/print/qrcodetracker')
def print_qrcodetracker():
    return_dict = {'success':False}

    try:
        context = get_label_context(request)
        logger.warning(context)
    except LookupError as e:
        return_dict['error'] = e.msg
        return return_dict

    import requests
    from PIL import Image, ImageOps
    from io import BytesIO

    r = requests.get('https://api.qrserver.com/v1/create-qr-code/', 
        params={'data':context['qr'],'size':'%sx%s' % (context['qrsize'],context['qrsize'])})
    logger.warning('REQUEST URL: %s' % (r.url))
    logger.warning(r)
    logger.warning('contents:')
    logger.warning(r.content)
    im = Image.open(BytesIO(r.content))

    im = im.convert('RGB')
    im.save('last-qrcode.png')

    if context['kind'] == ENDLESS_LABEL:
        rotate = 0 if context['orientation'] == 'standard' else 90
    elif context['kind'] in (ROUND_DIE_CUT_LABEL, DIE_CUT_LABEL):
        rotate = 'auto'

    if rotate == 0:
        i2 = Image.new('RGB', (696, context['qrsize']), '#ffffff')
    else:
        i2 = Image.new('RGB', (context['qrsize'], 696), '#ffffff')
#    i2 = ImageOps.expand(im, border=300, fill='#ffffff')
#    im = i2.crop((0,300, 696,300+context['qrsize']))

    # create multiple qrcodes on the label ... 
    xoffset = 0

    im3 = create_label_im(**context)

    im33 = im3.convert('RGB')
    im33.save('last-text-0.png')

    i2.paste(im3, (0, 0))

    im33 = i2.convert('RGB')
    im33.save('last-text-1.png')

    i2.paste(im, (0, 100))

    im33 = i2.convert('RGB')
    im33.save('last-text-2.png')

    context['width'] = 696
    context['height'] = context['qrsize']

    qlr = BrotherQLRaster(CONFIG['PRINTER']['MODEL'])
    create_label(qlr, i2, context['label_size'], threshold=context['threshold'], cut=True, rotate=rotate)

    if not DEBUG:
        try:
            be = BACKEND_CLASS(CONFIG['PRINTER']['PRINTER'])
            be.write(qlr.data)
            be.dispose()
            del be
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG: return_dict['data'] = str(qlr.data)
    return return_dict


@post('/api/print/image')
@get('/api/print/image')
def print_image():
    return_dict = {'success':False}

    logger.warning(request)

    try:
        #context = get_label_context(request)
        context = {}
        logger.warning(context)
    except LookupError as e:
        return_dict['error'] = e.msg
        return return_dict

    import requests
    from PIL import Image, ImageOps
    from io import BytesIO

    # load image from the upload request .. 
    
    print('file names:')
    for k in request.files.keys():
        print(k)

    upload = request.files.get('photos')
    name = upload.filename
    image_name = 'last-image-%s' % name
    if (os.path.exists(image_name)):
        os.remove(image_name)
    upload.save(image_name)

    im = Image.open(image_name)

    width, height = im.size

    i2 = Image.new('RGB', (696, height), '#ffffff')

    context['width'] = 696
    context['height'] = height

    rotate = 0 

    qlr = BrotherQLRaster(CONFIG['PRINTER']['MODEL'])

    if not DEBUG:
        try:
            be = BACKEND_CLASS(CONFIG['PRINTER']['PRINTER'])
            be.write(qlr.data)
            be.dispose()
            del be
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG: return_dict['data'] = str(qlr.data)
    return return_dict




@post('/api/print/text')
@get('/api/print/text')
def print_text():
    """
    API to print a label

    returns: JSON

    Ideas for additional URL parameters:
    - alignment
    """

    return_dict = {'success': False}

    try:
        context = get_label_context(request)
    except LookupError as e:
        return_dict['error'] = e.msg
        return return_dict

    if context['text'] is None:
        return_dict['error'] = 'Please provide the text for the label'
        return return_dict

    im = create_label_im(**context)
    im.save('sample-out.png')

    if context['kind'] == ENDLESS_LABEL:
        rotate = 0 if context['orientation'] == 'standard' else 90
    elif context['kind'] in (ROUND_DIE_CUT_LABEL, DIE_CUT_LABEL):
        rotate = 'auto'

    red = False
    if context['color'] == 'redandblack':
        red = True

    qlr = BrotherQLRaster(CONFIG['PRINTER']['MODEL'])
    create_label(qlr, im, context['label_size'], threshold=context['threshold'], cut=True, red=red, rotate=rotate)

    if not DEBUG:
        try:
            be = BACKEND_CLASS(CONFIG['PRINTER']['PRINTER'])
            be.write(qlr.data)
            be.dispose()
            del be
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG: return_dict['data'] = str(qlr.data)
    return return_dict

@route('/api/print/image', method='POST')
def print_image():
    """
    API to print a label from image source

    returns: JSON
    """

    return_dict = {'success': False}

    try:
        context = get_label_context(request)
    except LookupError as e:
        return_dict['error'] = e.msg
        return return_dict

    upload = request.files.get('upload')
    
    name, ext = os.path.splitext(upload.filename)
    if ext not in ('.png', '.jpg', '.jpeg'):
        return_dict['error'] = "File extension not allowed."
        return return_dict

    im = Image.open(io.BytesIO(upload.file.read()))
    
    if DEBUG: im.save('sample-out.png')

    if context['kind'] == ENDLESS_LABEL:
        rotate = 0 if context['orientation'] == 'standard' else 90
    elif context['kind'] in (ROUND_DIE_CUT_LABEL, DIE_CUT_LABEL):
        rotate = 'auto'

    qlr = BrotherQLRaster(CONFIG['PRINTER']['MODEL'])
    create_label(qlr, im, context['label_size'], dither=True, cut=True, rotate=rotate)

    if not DEBUG:
        try:
            be = BACKEND_CLASS(CONFIG['PRINTER']['PRINTER'])
            be.write(qlr.data)
            be.dispose()
            del be
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG: return_dict['data'] = str(qlr.data)
    return return_dict

def main():
    global DEBUG, FONTS, BACKEND_CLASS, CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default=False)
    parser.add_argument('--loglevel', type=lambda x: getattr(logging, x.upper()), default=False)
    parser.add_argument('--font-folder', default=False, help='folder for additional .ttf/.otf fonts')
    parser.add_argument('--default-label-size', default=False, help='Label size inserted in your printer. Defaults to 62.')
    parser.add_argument('--default-orientation', default=False, choices=('standard', 'rotated'), help='Label orientation, defaults to "standard". To turn your text by 90°, state "rotated".')
    parser.add_argument('--model', default=False, choices=models, help='The model of your printer (default: QL-500)')
    parser.add_argument('printer',  nargs='?', default=False, help='String descriptor for the printer to use (like tcp://192.168.0.23:9100 or file:///dev/usb/lp0)')
    args = parser.parse_args()

    if args.printer:
        CONFIG['PRINTER']['PRINTER'] = args.printer

    if args.port:
        PORT = args.port
    else:
        PORT = CONFIG['SERVER']['PORT']

    if args.loglevel:
        LOGLEVEL = args.loglevel
    else:
        LOGLEVEL = CONFIG['SERVER']['LOGLEVEL']

    if LOGLEVEL == 'DEBUG':
        DEBUG = True
    else:
        DEBUG = False

    if args.model:
        CONFIG['PRINTER']['MODEL'] = args.model

    if args.default_label_size:
        CONFIG['LABEL']['DEFAULT_SIZE'] = args.default_label_size

    if args.default_orientation:
        CONFIG['LABEL']['DEFAULT_ORIENTATION'] = args.default_orientation

    if args.font_folder:
        ADDITIONAL_FONT_FOLDER = args.font_folder
    else:
        ADDITIONAL_FONT_FOLDER = CONFIG['SERVER']['ADDITIONAL_FONT_FOLDER']


    logging.basicConfig(level=LOGLEVEL)

    try:
        selected_backend = guess_backend(CONFIG['PRINTER']['PRINTER'])
    except ValueError:
        parser.error("Couln't guess the backend to use from the printer string descriptor")
    BACKEND_CLASS = backend_factory(selected_backend)['backend_class']

    if CONFIG['LABEL']['DEFAULT_SIZE'] not in label_sizes:
        parser.error("Invalid --default-label-size. Please choose on of the following:\n:" + " ".join(label_sizes))

    FONTS = get_fonts()
    if ADDITIONAL_FONT_FOLDER:
        FONTS.update(get_fonts(ADDITIONAL_FONT_FOLDER))

    if not FONTS:
        sys.stderr.write("Not a single font was found on your system. Please install some or use the \"--font-folder\" argument.\n")
        sys.exit(2)

    for font in CONFIG['LABEL']['DEFAULT_FONTS']:
        try:
            FONTS[font['family']][font['style']]
            CONFIG['LABEL']['DEFAULT_FONTS'] = font
            logger.debug("Selected the following default font: {}".format(font))
            break
        except: pass
    if CONFIG['LABEL']['DEFAULT_FONTS'] is None:
        sys.stderr.write('Could not find any of the default fonts. Choosing a random one.\n')
        family =  random.choice(list(FONTS.keys()))
        style =   random.choice(list(FONTS[family].keys()))
        CONFIG['LABEL']['DEFAULT_FONTS'] = {'family': family, 'style': style}
        sys.stderr.write('The default font is now set to: {family} ({style})\n'.format(**CONFIG['LABEL']['DEFAULT_FONTS']))

    run(host=CONFIG['SERVER']['HOST'], port=PORT, debug=DEBUG)

if __name__ == "__main__":
    main()
