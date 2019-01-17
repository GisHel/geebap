#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Module to implement scores in the Bap Image Composition.

Scores can be computed using a single image, for example a score for percentage
of masked pixels, or using a collection, for example a score for outliers.

Each score must have an `apply` method which first argument must be an
ImageCollection and must return the collection with score computed in each
image. This method should be an staticmethod and not depend on any external
library except Earth Engine API.

Each score must have an `compute` method which first argument must be an Image
and return the same Image with the score computed. This method should be an
staticmethod and not depend on any external library except Earth Engine API.

If the `compute` method is borrowed from another package it can be passed by
overwriting it. It should be use only internally by `apply` method, and `apply`
method should be the only one used by the BAP process.
"""
import ee

import ee.data
if not ee.data._initialized: ee.Initialize()

from . import satcol, functions, season
from geetools import tools, composite

from .expressions import Expression
from abc import ABCMeta, abstractmethod
from .regdec import *

__all__ = []
factory = {}


class Score(object):
    ''' Abstract Base class for scores '''
    __metaclass__ = ABCMeta

    def __init__(self, name="score", range_in=None, formula=None,
                 range_out=(0, 1), sleep=0, **kwargs):
        """ Abstract Base Class for scores

        :param name: score's name
        :type name: str
        :param range_in: score's range
        :type range_in: tuple
        :param sleep: time to wait until to compute the next score
        :type sleep: int
        :param formula: formula to use for computing the score
        :type formula: Expression
        :param range_out: Adjust the output to this range
        :type range_out: tuple
        """
        self.name = name
        self.range_in = range_in
        self.formula = formula
        self.range_out = range_out
        self.sleep = sleep

    @property
    def normalize(self):
        return True

    @property
    def max(self):
        return self.range_out[1]

    @property
    def min(self):
        return self.range_out[0]

    def adjust(self):
        if self.range_out != (0, 1):
            return lambda img: tools.image.parametrize(img, (0, 1),
                                                       self.range_out,
                                                       [self.name])
        else:
            return lambda x: x

    @abstractmethod
    def map(self, **kwargs):
        """ Abstract map method to use in ImageCollection.map() """
        def wrap(img):
            return img
        return wrap

    @staticmethod
    def compute(img, **kwargs):
        """ Abstract compute method. This method is supposed to be the one to
        compute the score
        """
        def wrap(img):
            return img
        return wrap

    @staticmethod
    def apply(collection, **kwargs):
        return collection

    def empty(self, img):
        """ Make an empty score band. All pixels will have zero value """
        i = ee.Image.constant(0).select([0], [self.name]).toFloat()
        return img.addBands(i)


@register(factory)
@register_all(__all__)
class CloudScene(Score):
    """ Cloud cover percent score for the whole scene. Default name for the
    resulting band will be 'score-cld-esc'.

    :param name: name of the resulting band
    :type name: str
    """
    def __init__(self, name="score-cld-esc", **kwargs):
        super(CloudScene, self).__init__(**kwargs)
        self.range_in = (0, 100)
        self.name = name

        self.formula = Expression.Exponential(rango=self.range_in,
                                              normalizar=self.normalize,
                                              **kwargs)

    @staticmethod
    def compute(img, **kwargs):
        formula = kwargs.get('formula')
        fmap = kwargs.get('fmap')
        name = kwargs.get('name')
        cloud_cover = kwargs.get('cloud_cover')

        if cloud_cover:
            func = formula.map(name,
                               prop=cloud_cover,
                               map=fmap)
            return func(img)
        else:
            return img

    @staticmethod
    def apply(collection, **kwargs):
        return collection.map(lambda img: CloudScene.compute(img, **kwargs))

    def map(self, col, **kwargs):
        """

        :param col: collection
        :type col: satcol.Collection
        """
        if col.clouds_fld:
            return lambda img: self.compute(img, col.clouds_fld)
        else:
            return self.empty


@register(factory)
@register_all(__all__)
class CloudDist(Score):
    """ Score for the distance to the nearest cloud. Default name will be
    'score-cld-dist'

    :param unit: Unit to use in the distance kernel. Options: meters,
        pixels
    :type unit: str
    :param dmax: Maximum distance to calculate the score. If the pixel is
        further than dmax, the score will be 1.
    :type dmax: int
    :param dmin: Minimum distance.
    :type dmin: int
    """
    kernels = {"euclidean": ee.Kernel.euclidean,
               "manhattan": ee.Kernel.manhattan,
               "chebyshev": ee.Kernel.chebyshev
               }

    def __init__(self, dmax=600, dmin=0, unit="meters", name="score-cld-dist",
                 kernel='euclidean', **kwargs):
        super(CloudDist, self).__init__(**kwargs)
        self.kernel = kernel  # kwargs.get("kernel", "euclidean")
        self.unit = unit
        self.dmax = dmax
        self.dmin = dmin
        self.name = name
        self.range_in = (dmin, dmax)
        self.sleep = kwargs.get("sleep", 10)

    # GEE
    @property
    def dmaxEE(self):
        return ee.Image.constant(self.dmax)

    @property
    def dminEE(self):
        return ee.Image.constant(self.dmin)

    def kernelEE(self):
        fkernel = CloudDist.kernels[self.kernel]
        return fkernel(radius=self.dmax, units=self.unit)

    def generate_score(self, image, bandmask):
        # ceros = image.select([0]).eq(0).Not()

        cloud_mask = image.mask().select(bandmask)

        # Compute distance to the mask (inverse)
        distance = cloud_mask.Not().distance(self.kernelEE())

        # Mask out pixels that are further than d_max
        clip_max_masc = distance.lte(self.dmaxEE)
        distance = distance.updateMask(clip_max_masc)

        # Mask out initial mask
        distance = distance.updateMask(cloud_mask)

        # AGREGO A LA IMG LA BANDA DE DISTANCIAS
        # img = img.addBands(distancia.select([0],["dist"]).toFloat())

        # Compute score

        c = self.dmaxEE.subtract(self.dminEE).divide(ee.Image(2))
        b = distance.min(self.dmaxEE)
        a = b.subtract(c).multiply(ee.Image(-0.2)).exp()
        e = ee.Image(1).add(a)

        pjeDist = ee.Image(1).divide(e)

        # Inverse mask to add later
        masc_inv = pjeDist.mask().Not()

        # apply mask2zero (all masked pixels are converted to 0)
        pjeDist = pjeDist.unmask()
        #pjeDist = pjeDist.mask().where(1, pjeDist)

        # Add the inverse mask to the distance image
        pjeDist = pjeDist.add(masc_inv)

        # Apply the original mask
        pjeDist = pjeDist.updateMask(cloud_mask)

        return pjeDist

    def map(self, col, **kwargs):
        bandmask = col.bandmask if col.bandmask else 0
        def wrap(img):
            score_img = self.generate_score(img, bandmask)
            adjusted_score = self.adjust()(score_img)
            renamed_image = adjusted_score.select([0], [self.name])
            return img.addBands(renamed_image)
        return wrap

    def map_old(self, col, **kwargs):
        """ Mapping function

        :param col: Collection
        :type col: satcol.Collection
        """
        nombre = self.name
        # bandmask = self.bandmask
        bandmask = col.bandmask
        kernelEE = self.kernelEE()
        dmaxEE = self.dmaxEE
        dminEE = self.dminEE
        ajuste = self.adjust()

        def wrap(img):
            """ calcula el puntaje de distancia a la nube.

            Cuando d >= dmax --> pdist = 1
            Propósito: para usar en la función map() de GEE
            Objetivo:

            :return: la propia imagen con una band agregada llamada 'pdist'
            :rtype: ee.Image
            """
            # Selecciono los pixeles con valor distinto de cero
            ceros = img.select([0]).eq(0).Not()

            masc_nub = img.mask().select(bandmask)

            # COMPUTO LA DISTANCIA A LA MASCARA DE NUBES (A LA INVERSA)
            distancia = masc_nub.Not().distance(kernelEE)

            # BORRA LOS DATOS > d_max (ESTO ES PORQUE EL KERNEL TOMA LA DIST
            # DIAGONAL TAMB)
            clip_max_masc = distancia.lte(dmaxEE)
            distancia = distancia.updateMask(clip_max_masc)

            # BORRA LOS DATOS = 0
            distancia = distancia.updateMask(masc_nub)

            # AGREGO A LA IMG LA BANDA DE DISTANCIAS
            # img = img.addBands(distancia.select([0],["dist"]).toFloat())

            # COMPUTO EL PUNTAJE (WHITE)

            c = dmaxEE.subtract(dminEE).divide(ee.Image(2))
            b = distancia.min(dmaxEE)
            a = b.subtract(c).multiply(ee.Image(-0.2)).exp()
            e = ee.Image(1).add(a)

            pjeDist = ee.Image(1).divide(e)

            # TOMA LA MASCARA INVERSA PARA SUMARLA DESP
            masc_inv = pjeDist.mask().Not()

            # TRANSFORMA TODOS LOS VALORES ENMASCARADOS EN 0
            pjeDist = pjeDist.mask().where(1, pjeDist)

            # SUMO LA MASC INVERSA A LA IMG DE DISTANCIAS
            pjeDist = pjeDist.add(masc_inv)

            # VUELVO A ENMASCARAR LAS NUBES
            pjeDist = pjeDist.updateMask(masc_nub)

            # DE ESTA FORMA OBTENGO VALORES = 1 SOLO DONDE LA DIST ES > 50
            # DONDE LA DIST = 0 ESTA ENMASCARADO

            newimg = img.addBands(pjeDist.select([0], [nombre]).toFloat())
            newimg_masked = newimg.updateMask(ceros)

            return ajuste(newimg_masked)
        return wrap

    def mask_kernel(self, img):
        """ Mask out pixels within `dmin` and `dmax` properties of the score."""
        masc_nub = img.mask().select(self.bandmask)

        # COMPUTO LA DISTANCIA A LA MASCARA DE NUBES (A LA INVERSA)
        distancia = masc_nub.Not().distance(self.kernelEE())

        # BORRA LOS DATOS > d_max (ESTO ES PORQUE EL KERNEL TOMA LA DIST
        # DIAGONAL TAMB)
        # clip_max_masc = distancia.lte(self.dmaxEE())
        # distancia = distancia.updateMask(clip_max_masc)

        # BORRA LOS DATOS = 0
        distancia = distancia.updateMask(masc_nub)

        # TRANSFORMO TODOS LOS PIX DE LA DISTANCIA EN 0 PARA USARLO COMO
        # MASCARA BUFFER
        buff = distancia.gte(ee.Image(0))
        buff = buff.mask().where(1, buff)
        buff = buff.Not()

        # APLICO LA MASCARA BUFFER
        return img.updateMask(buff)


@register(factory)
@register_all(__all__)
class Doy(Score):
    """ Score for the 'Day of the Year (DOY)'

    :param formula: Formula to use
    :type formula: Expression
    :param season: Growing season (holds a `doy` attribute)
    :type season: season.Season
    :param name: name for the resulting band
    :type name: str
    """
    def __init__(self, formula=Expression.Normal, name="score-doy",
                 season=season.Season.Growing_South(), **kwargs):
        super(Doy, self).__init__(**kwargs)
        # PARAMETROS
        self.doy_month = season.doy_month
        self.doy_day = season.doy_day

        self.ini_month = season.ini_month
        self.ini_day = season.ini_day

        # FACTOR DE AGUSADO DE LA CURVA GAUSSIANA
        # self.ratio = float(ratio)

        # FORMULA QUE SE USARA PARA EL CALCULO
        self.exp = formula

        self.name = name

    # DOY
    def doy(self, year):
        """ DOY: Day Of Year. Most representative day of the year for the
        growing season

        :param year: Year
        :type year: int
        :return: the doy
        :rtype: ee.Date
        """
        d = "{}-{}-{}".format(year, self.doy_month, self.doy_day)
        return ee.Date(d)

    def ini_date(self, year):
        """ Initial date

        :param year: Year
        :type year: int
        :return: initial date
        :rtype: ee.Date
        """
        d = "{}-{}-{}".format(year - 1, self.ini_month, self.ini_day)
        return ee.Date(d)

    def end_date(self, year):
        """ End date

        :param year: Year
        :type year: int
        :return: end date
        :rtype: ee.Date
        """
        dif = self.doy(year).difference(self.ini_date(year), "day")
        return self.doy(year).advance(dif, "day")

    def doy_range(self, year):
        """ Number of days since `ini_date` until `end_date`

        :param year: Year
        :type year: int
        :return: Doy range
        :rtype: ee.Number
        """
        return self.end_date(year).difference(self.ini_date(year), "day")

    def sequence(self, year):
        return ee.List.sequence(1, self.doy_range(year).add(1))

    # CANT DE DIAS DEL AÑO ANTERIOR
    def last_day(self, year):
        return ee.Date.fromYMD(ee.Number(year - 1), 12, 31).getRelative(
            "day", "year")

    def mean(self, year):
        """ Mean

        :return: Valor de la mean en un objeto de Earth Engine
        :rtype: ee.Number
        """
        return ee.Number(self.sequence(year).reduce(ee.Reducer.mean()))

    def std(self, year):
        """ Standar deviation

        :return:
        :rtype: ee.Number
        """
        return ee.Number(self.sequence(year).reduce(ee.Reducer.stdDev()))

    def distance_to_ini(self, date, year):
        """ Distance in days between the initial date and the given date for
        the given year (season)

        :param date: date to compute the 'relative' position
        :type date: ee.Date
        :param year: the year of the season
        :type year: int
        :return: distance in days between the initial date and the given date
        :rtype: int
        """
        ini = self.ini_date(year)
        return date.difference(ini, "day")

    # VARIABLES DE EE
    # @property
    # def castigoEE(self):
    #    return ee.Number(self.castigo)

    def ee_year(self, year):
        return ee.Number(year)

    def map(self, year, **kwargs):
        """

        :param year: central year
        :type year: int
        """
        media = self.mean(year).getInfo()
        std = self.std(year).getInfo()
        ran = self.doy_range(year).getInfo()
        self.rango_in = (1, ran)

        # exp = Expression.Normal(mean=mean, std=std)
        expr = self.exp(media=media, std=std,
                        rango=self.rango_in, normalizar=self.normalize, **kwargs)

        def transform(prop):
            date = ee.Date(prop)
            pos = self.distance_to_ini(date, year)
            return pos

        return expr.map(self.name, prop="system:time_start", eval=transform,
                        map=self.adjust(), **kwargs)


@register(factory)
@register_all(__all__)
class AtmosOpacity(Score):
    """ Score for 'Atmospheric Opacity'

    :param range_in: Range of variation for the atmos opacity
    :type range_in: tuple
    :param formula: Distribution formula
    :type formula: Expression
    """
    def __init__(self, range_in=(100, 300), formula=Expression.Exponential,
                 name="score-atm-op", **kwargs):
        super(AtmosOpacity, self).__init__(**kwargs)
        self.range_in = range_in
        self.formula = formula
        self.name = name

    @property
    def expr(self):
        expresion = self.formula(rango=self.range_in)
        return expresion

    def map(self, col, **kwargs):
        """

        :param col: collection
        :type col: satcol.Collection
        """
        if col.ATM_OP:
            return self.expr.map(name=self.name,
                                 band=col.ATM_OP,
                                 map=self.adjust(),
                                 **kwargs)
        else:
            return self.empty


@register(factory)
@register_all(__all__)
class MaskPercent(Score):
    """ This score represents the 'masked pixels cover' for a given area.
    It uses a ee.Reducer so it can consume much EE capacity

    :param band: band of the image that holds the masked pixels
    :type band: str
    :param maxPixels: same param of ee.Reducer
    :type maxPixels: int
    :param include_zero: include pixels with zero value as mask
    :type include_zero: bool
    """
    @staticmethod
    def core(mask_image, geometry, scale=1000, band_name='score-maskper',
             max_pixels=1e13):
        """ Core function for Mask Percent Score. Has no dependencies in geebap
        module

        :param mask_image: ee.Image holding the mask
        :type mask_image: ee.Image
        :param geometry: the score will be computed inside this geometry
        :type geometry: ee.Geometry or ee.Feature
        :param scale: the scale of the mask
        :type scale: int
        :param band_name: the name of the resulting band
        :type band_name: str
        :return: An image with one band that holds the percentage of pixels
            with value 0 (not 1) over the total pixels inside the geometry, and
            a property with the same name as the assigned for the band with the
            same percentage
        :rtype: ee.Image
        """
        # get projection
        projection = mask_image.projection()

        # get band name
        band = ee.String(mask_image.bandNames().get(0))

        # Make an image with all ones
        ones_i = ee.Image.constant(1).reproject(projection).rename(band)

        # manage geometry types
        if isinstance(geometry, (ee.Feature, ee.FeatureCollection)):
            geometry = geometry.geometry()

        # Get total number of pixels
        ones = ones_i.reduceRegion(
            reducer= ee.Reducer.count(),
            geometry= geometry,
            scale= scale,
            maxPixels= max_pixels).get(band)
        ones = ee.Number(ones)

        # select first band, unmask and get the inverse
        mask_image = mask_image.select([0])
        mask = mask_image.mask()
        mask_not = mask.Not()
        image_to_compute = mask.updateMask(mask_not)

        # Get number of zeros in the given mask_image
        zeros_in_mask =  image_to_compute.reduceRegion(
            reducer= ee.Reducer.count(),
            geometry= geometry,
            scale= scale,
            maxPixels= max_pixels).get(band)
        zeros_in_mask = ee.Number(zeros_in_mask)

        percentage = tools.number.trim_decimals(zeros_in_mask.divide(ones), 4)

        # Make score inverse to percentage
        score = ee.Number(1).subtract(percentage)

        percent_image = ee.Image.constant(score) \
            .select([0], [band_name]).set(band_name, score)

        return percent_image.clip(geometry)


    def __init__(self, band=None, name="score-maskper", maxPixels=1e13,
                 include_zero=True, **kwargs):
        super(MaskPercent, self).__init__(**kwargs)
        self.band = band
        self.maxPixels = maxPixels
        self.name = name
        self.include_zero = include_zero  # TODO
        self.sleep = kwargs.get("sleep", 30)

        self.zero = ee.Number(1) if self.include_zero else ee.Number(0)

    def compute(self, col, geom=None, **kwargs):
        """ Compute MaskPercent score

        :param col: collection
        :type col: satcol.Collection
        :param geom: geometry of the area
        :type geom: ee.Geometry, ee.Feature
        :return:
        """
        nombre = self.name
        banda = self.band if self.band else col.bandmask
        ajuste = self.adjust()
        scale = col.bandscale.get(banda)

        if banda:
            def wrap(img):

                def if_true():
                    # Select pixels with value different to zero
                    ceros = img.select(banda).neq(0)
                    return ceros.unmask()

                def if_false():
                    return img.select(banda).mask()

                themask = ee.Algorithms.If(self.zero,
                                           if_true(),
                                           if_false())

                # g = region if region else img.geometry()
                g = geom if geom else img.geometry()

                mask = ee.Image(themask)

                image = img.select(banda).updateMask(mask)

                imgpor = MaskPercent.core(image, g, scale, nombre,
                                          self.maxPixels)

                return ajuste(imgpor)
        else:
            wrap = self.empty

        return wrap

    def map(self, col, geom=None, **kwargs):
        def wrap(img):
            score = self.compute(col, geom, **kwargs)(img)
            prop = score.get(self.name)
            return img.addBands(score).set(self.name, prop)
        return wrap


@register(factory)
@register_all(__all__)
class Satellite(Score):
    """ Score for the satellite

    :param rate: 'amount' of the score that will be taken each step of the
        available satellite list
    :type rate: float
    """
    def __init__(self, rate=0.05, name="score-sat", **kwargs):
        super(Satellite, self).__init__(**kwargs)
        self.name = name
        self.rate = rate

    def map(self, col, **kwargs):
        """
        :param col: Collection
        :type col: satcol.Collection
        """
        nombre = self.name
        theid = col.ID
        ajuste = self.adjust()

        def wrap(img):
            # CALCULA EL PUNTAJE:
            # (tamaño - index)/ tamaño
            # EJ: [1, 0.66, 0.33]
            # pje = size.subtract(index).divide(size)
            ##

            # Selecciono los pixeles con valor distinto de cero
            ceros = img.select([0]).eq(0).Not()

            a = img.date().get("year")

            # LISTA DE SATELITES PRIORITARIOS PARA ESE AÑO
            # lista = season.PriorTempLandEE(ee.Number(a)).listaEE
            lista = season.SeasonPriority.ee_relation.get(a.format())
            # UBICA AL SATELITE EN CUESTION EN LA LISTA DE SATELITES
            # PRIORITARIOS, Y OBTIENE LA POSICION EN LA LISTA
            index = ee.List(lista).indexOf(ee.String(theid))

            ## OPCION 2
            # 1 - (0.05 * index)
            # EJ: [1, 0.95, 0.9]
            factor = ee.Number(self.rate).multiply(index)
            pje = ee.Number(1).subtract(factor)
            ##

            # ESCRIBE EL PUNTAJE EN LOS METADATOS DE LA IMG
            img = img.set(nombre, pje)

            # CREA LA IMAGEN DE PUNTAJES Y LA DEVUELVE COMO RESULTADO
            pjeImg = ee.Image(pje).select([0], [nombre]).toFloat()
            return ajuste(img.addBands(pjeImg).updateMask(ceros))
        return wrap


@register(factory)
@register_all(__all__)
class Outliers(Score):
    """ Score for outliers

    Compute a pixel based score regarding to its 'outlier' condition. It
    can use more than one band.

    To see an example, run `test_outliers.py`

    :param bands: name of the bands to compute the outlier score
    :type bands: tuple
    :param process: Statistic to detect the outlier
    :type process: str
    :param dist: 'distance' to be considered outlier. If the chosen process
        is 'mean' the distance is in 'standar deviation' else if it is
        'median' the distance is in 'percentage/100'. Example:

        dist=1 -> min=0, max=100

        dist=0.5 -> min=25, max=75

        etc

    :type dist: int
    """

    def __init__(self, bands, process="median", name="score-outlier",
                 dist=0.7, **kwargs):
        super(Outliers, self).__init__(**kwargs)

        # TODO: el param bands esta mas relacionado a la coleccion... pensarlo mejor..
        # if not (isinstance(bands, tuple) or isinstance(bands, list)):
        #    raise ValueError("El parametro 'bands' debe ser una tupla o lista")
        self.bands = bands
        self.bands_ee = ee.List(bands)

        # self.col = col.select(self.bands)
        self.process = process
        # self.distribution = kwargs.get("distribution", "discreta")
        # TODO: distribution
        self.dist = dist
        '''
        self.minVal = kwargs.get("min", 0)
        self.maxVal = kwargs.get("max", 1)
        self.rango_final = (self.minVal, self.maxVal)
        self.rango_orig = (0, 1)
        '''
        self.range_in = (0, 1)
        # self.bandslength = float(len(bands))
        # self.increment = float(1/self.bandslength)
        self.name = name
        self.sleep = kwargs.get("sleep", 10)

        # TODO: create `min` and `max` properties depending on the chosen process

    @property
    def dist(self):
        return self._dist

    @dist.setter
    def dist(self, val):
        val = 0.7 if val is not isinstance(val, float) else val
        if self.process == 'mean':
            self._dist = val
        elif self.process == 'median':
            # Normalize distance to median
            val = 0 if val < 0 else val
            val = 1 if val > 1 else val
            self._dist = int(val*50)

    @property
    def bandslength(self):
        return float(len(self.bands))

    @property
    def increment(self):
        return float(1 / self.bandslength)

    def map(self, colEE, **kwargs):
        """
        :param colEE: Earth Engine collection to process
        :type colEE: ee.ImageCollection
        :return:
        :rtype: ee.Image
        """
        nombre = self.name
        bandas = self.bands_ee
        rango_orig = self.range_in
        rango_fin = self.range_out
        incremento = self.increment
        col = colEE.select(bandas)
        process = self.process

        # MASK PIXELS = 0 OUT OF EACH IMAGE OF THE COLLECTION
        def masktemp(img):
            m = img.neq(0)
            return img.updateMask(m)
        coltemp = col.map(masktemp)

        if process == "mean":
            media = ee.Image(coltemp.mean())
            std = ee.Image(col.reduce(ee.Reducer.stdDev()))
            stdXdesvio = std.multiply(self.dist)

            mmin = media.subtract(stdXdesvio)
            mmax = media.add(stdXdesvio)

        elif process == "median":
            # mediana = ee.Image(col.median())
            min = ee.Image(coltemp.reduce(ee.Reducer.percentile([50-self.dist])))
            max = ee.Image(coltemp.reduce(ee.Reducer.percentile([50+self.dist])))

            mmin = min
            mmax = max

        # print(mmin.getInfo())
        # print(mmax.getInfo())

        def wrap(img):

            # Selecciono los pixeles con valor distinto de cero
            # ceros = img.select([0]).eq(0).Not()
            ceros = img.neq(0)

            # ORIGINAL IMAGE
            img_orig = img

            # SELECT BANDS
            img_proc = img.select(bandas)

            # CONDICION
            condicion_adentro = (img_proc.gte(mmin)
                                 .And(img_proc.lte(mmax)))

            pout = functions.simple_rename(condicion_adentro, suffix="pout")

            suma = tools.image.sumBands(pout, nombre)

            final = suma.select(nombre).multiply(ee.Image(incremento))

            parametrizada = tools.image.parametrize(final, rango_orig,
                                                    rango_fin)

            return img_orig.addBands(parametrizada)#.updateMask(ceros)
        return wrap


@register(factory)
@register_all(__all__)
class Index(Score):
    """ Score for a vegetation index. As higher the index value, higher the
    score.

    :param index: name of the vegetation index. Can be 'ndvi', 'evi' or 'nbr'
    :type index: str
    """
    def __init__(self, index="ndvi", name="score-index", **kwargs):
        super(Index, self).__init__(**kwargs)
        self.index = index
        self.range_in = kwargs.get("range_in", (-1, 1))
        self.name = name

    def map(self, **kwargs):
        ajuste = self.adjust()
        def wrap(img):
            ind = img.select([self.index])
            p = tools.image.parametrize(ind, self.range_in, self.range_out)
            p = p.select([0], [self.name])
            return ajuste(img.addBands(p))
        return wrap


@register(factory)
@register_all(__all__)
class MultiYear(Score):
    """ Score for a multiyear (multiseason) composite. Suppose you create a
    single composite for 2002 but want to use images from 2001 and 2003. To do
    that you have to indicate in the creation of the Bap object, but you want
    to prioritize the central year (2002). To do that you have to include this
    score in the score's list.

    :param main_year: central year
    :type main_year: int
    :param season: main season
    :type season: season.Season
    :param ratio: how much score will be taken each year. In the example would
        be 0.95 for 2001, 1 for 2002 and 0.95 for 2003
    :type ration: float
    """

    def __init__(self, main_year, season, ratio=0.05, name="score-multi",
                 **kwargs):
        super(MultiYear, self).__init__(**kwargs)
        self.main_year = main_year
        self.season = season
        self.ratio = ratio
        self.name = name

    def map(self, **kwargs):
        a = self.main_year
        ajuste = self.adjust()

        def wrap(img):

            # Selecciono los pixeles con valor distinto de cero
            ceros = img.select([0]).eq(0).Not()

            # FECHA DE LA IMAGEN
            imgdate = ee.Date(img.date())

            diff = ee.Number(self.season.year_diff_ee(imgdate, a))

            pje1 = ee.Number(diff).multiply(ee.Number(self.ratio))
            pje = ee.Number(1).subtract(pje1)

            imgpje = ee.Image.constant(pje).select([0], [self.name])

            # return funciones.pass_date(img, img.addBands(imgpje))
            return ajuste(img.addBands(imgpje).updateMask(ceros))
        return wrap


class Threshold_old(Score):
    def __init__(self, band=None, threshold=None, name='score-thres',
                 **kwargs):
        """

        :param band:
        :param threshold:
        :type threshold: tuple or list
        """
        super(Threshold_old, self).__init__(**kwargs)

        self.band = band
        self.threshold = threshold
        self.name = name

    def map(self, **kwargs):
        min = self.threshold[0]
        max = self.threshold[1]

        # TODO: handle percentage values like ('10%', '20%')

        def create_number(value):
            if isinstance(value, int) or isinstance(value, float):
                return ee.Number(int(value))
            elif isinstance(value, str):
                conversion = int(value)
                return ee.Number(conversion)

        min = create_number(min)
        max = create_number(max)

        def wrap_minmax(img):
            selected_band = img.select(self.band)
            upper = selected_band.gte(max)
            lower = selected_band.lte(min)
            
            score = selected_band.where(upper, 0)
            score = score.where(lower, 0)
            score = score.where(score.neq(0), 1)

            score = score.select([0], [self.name])
            
            return img.addBands(score)

        def wrap_min(img):
            selected_band = img.select(self.band)            
            lower = selected_band.lte(min)

            score = selected_band.where(lower, 0)
            score = score.select([0], [self.name])

            return img.addBands(score)

        def wrap_max(img):
            selected_band = img.select(self.band)
            upper = selected_band.gte(min)

            score = selected_band.where(upper, 0)
            score = score.select([0], [self.name])

            return img.addBands(score)

        # MULTIPLE DISPATCH?

        if min and max:
            return wrap_minmax
        elif not min:
            return wrap_max
        else:
            return wrap_min


@register(factory)
@register_all(__all__)
class Threshold(Score):
    def __init__(self, bands=None, name='score-thres',
                 **kwargs):
        """ Compute a threshold score, where values less than the min value or
        greater than the max value will have zero score

        :param bands: Dict of dicts as follows

            {'band_name':{'min':value, 'max':value}, 'band_name2'...}

            :values: must be numbers

            If 'min' is not set, will be automatically set to 0 and if 'max'
            is not set, will be automatically set to 1.

        :type bands: dict
        :param threshold:
        :type threshold: tuple or list
        """
        super(Threshold, self).__init__(**kwargs)

        self.bands = bands
        self.name = name

    def compute(self, col, **kwargs):
        # TODO: handle percentage values like ('10%', '20%')

        # If no bands relation is passed, uses the one stored in the collection
        band_relation = self.bands if self.bands else col.threshold

        # As each band must have a 'min' and 'max' if it is not specified,
        # it is completed with None. This way, it can be catched by ee.Algorithms.If
        for key, val in band_relation.items():
            val.setdefault('min', 0)
            val.setdefault('max', 1)

        bands = band_relation.keys()
        bands_ee = ee.List(bands)
        relation_ee = ee.Dictionary(band_relation)
        length = ee.Number(bands_ee.size())
        # step = ee.Number(1).divide(length)

        def wrap(img):
            def compute_score(band, first):
                score_complete = ee.Image(first)
                # score_img = score_complete.select([band])
                img_band = img.select([band])
                min = ee.Dictionary(relation_ee.get(band)).get('min')  # could be None
                max = ee.Dictionary(relation_ee.get(band)).get('max')  # could be None

                score_min = img_band.gte(ee.Image.constant(min))
                score_max = img_band.lte(ee.Image.constant(max))

                final_score = score_min.And(score_max)  # Image with one band

                return tools.image.replace(score_complete, band, final_score)

            scores = ee.Image(bands_ee.iterate(compute_score,
                                               tools.image.empty(0, bands)))
            # parametrized = scores.multiply(self.step)
            final_score = tools.image.sumBands(scores, name=self.name)\
                               .divide(ee.Image.constant(length))

            return final_score
        return wrap

    def map(self, col, **kwargs):
        def wrap(img):
            score = self.compute(col, **kwargs)(img).select(self.name)
            return img.addBands(score)

        return wrap


@register(factory)
@register_all(__all__)
class Medoid(Score):
    def __init__(self, name='score-medoid'):
        self.name = name

    def compute(self, *args, **kwargs):
        return composite.medoid_score()
