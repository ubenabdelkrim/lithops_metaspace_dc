from io import BytesIO

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from concurrent.futures import ThreadPoolExecutor

from annotation_pipeline.utils import ds_dims, get_pixel_indices, serialise, deserialise, read_cloud_object_with_retry, PipelineStats
from annotation_pipeline.validate import make_compute_image_metrics, formula_image_metrics
ISOTOPIC_PEAK_N = 4


class ImagesManager:
    min_memory_allowed = 64 * 1024 ** 2  # 64MB

    def __init__(self, storage, max_formula_images_size):
        if max_formula_images_size < self.__class__.min_memory_allowed:
            raise Exception(f'There isn\'t enough memory to generate images, consider increasing runtime_memory.')

        self.formula_metrics = {}
        self.formula_images = {}
        self.cloud_objs = []

        self._formula_images_size = 0
        self._max_formula_images_size = max_formula_images_size
        self._storage = storage
        self._partition = 0

    def __call__(self, f_i, f_metrics, f_images):
        self.add_f_metrics(f_i, f_metrics)
        self.add_f_images(f_i, f_images)

    @staticmethod
    def images_size(f_images):
        return sum(img.data.nbytes + img.row.nbytes + img.col.nbytes for img in f_images if img is not None)

    def add_f_images(self, f_i, f_images):
        self.formula_images[f_i] = f_images
        self._formula_images_size += ImagesManager.images_size(f_images)
        if self._formula_images_size > self._max_formula_images_size:
            self.save_images()
            self.formula_images.clear()
            self._formula_images_size = 0

    def add_f_metrics(self, f_i, f_metrics):
        self.formula_metrics[f_i] = f_metrics

    def save_images(self):
        if self.formula_images:
            print(f'Saving {len(self.formula_images)} images')
            cloud_obj = self._storage.put_cloudobject(serialise(self.formula_images))
            self.cloud_objs.append(cloud_obj)
            self._partition += 1
        else:
            print(f'No images to save')

    def finish(self):
        self.save_images()
        self.formula_images.clear()
        self._formula_images_size = 0
        return self.cloud_objs


def gen_iso_images(sp_inds, sp_mzs, sp_ints, centr_df, nrows, ncols, ppm=3, min_px=1):
    # assume sp data is sorted by mz order ascending
    # assume centr data is sorted by mz order ascending

    centr_f_inds = centr_df.formula_i.values
    centr_p_inds = centr_df.peak_i.values
    centr_mzs = centr_df.mz.values
    centr_ints = centr_df.int.values

    def yield_buffer(buffer):
        while len(buffer) < ISOTOPIC_PEAK_N:
            buffer.append((buffer[0][0], len(buffer) - 1, 0, None))
        buffer = np.array(buffer)
        buffer = buffer[buffer[:, 1].argsort()]  # sort order by peak ascending
        buffer = pd.DataFrame(buffer, columns=['formula_i', 'peak_i', 'centr_ints', 'image'])
        buffer.sort_values('peak_i', inplace=True)
        return buffer.formula_i[0], buffer.centr_ints, buffer.image

    if len(sp_inds) > 0:
        lower = centr_mzs - centr_mzs * ppm * 1e-6
        upper = centr_mzs + centr_mzs * ppm * 1e-6
        lower_idx = np.searchsorted(sp_mzs, lower, 'l')
        upper_idx = np.searchsorted(sp_mzs, upper, 'r')
        ranges_df = pd.DataFrame({'formula_i': centr_f_inds, 'lower_idx': lower_idx, 'upper_idx': upper_idx}).sort_values('formula_i')

        buffer = []
        for df_index, df_row in ranges_df.iterrows():
            if len(buffer) != 0 and buffer[0][0] != centr_f_inds[df_index]:
                yield yield_buffer(buffer)
                buffer = []

            l, u = df_row['lower_idx'], df_row['upper_idx']
            m = None
            if u - l >= min_px:
                data = sp_ints[l:u]
                inds = sp_inds[l:u]
                row_inds = inds / ncols
                col_inds = inds % ncols
                m = coo_matrix((data, (row_inds, col_inds)), shape=(nrows, ncols), copy=True)
            buffer.append((centr_f_inds[df_index], centr_p_inds[df_index], centr_ints[df_index], m))

        if len(buffer) != 0:
            yield yield_buffer(buffer)


def read_ds_segment(cobject, hybrid_impl, storage):
    data = read_cloud_object_with_retry(storage, cobject, deserialise)

    if isinstance(data, list):
        if isinstance(data[0], np.ndarray):
            data = np.concatenate(data)
        else:
            data = pd.concat(data, ignore_index=True, sort=False)

    if isinstance(data, np.ndarray):
        data = pd.DataFrame({
            'mz': data[:, 1],
            'int': data[:, 2],
            'sp_i': data[:, 0],
        })

    return data


def read_ds_segments(ds_segms_cobjects, ds_segms_len, pw_mem_mb, ds_segm_size_mb,
                     ds_segm_dtype, hybrid_impl, storage):

    ds_segms_mb = len(ds_segms_cobjects) * ds_segm_size_mb
    safe_mb = 512
    read_memory_mb = ds_segms_mb + safe_mb
    if read_memory_mb > pw_mem_mb:
        raise Exception(f'There isn\'t enough memory to read dataset segments, consider increasing runtime_memory for at least {read_memory_mb} mb.')

    safe_mb = 1024
    concat_memory_mb = ds_segms_mb * 2 + safe_mb
    if concat_memory_mb > pw_mem_mb:
        print('Using pre-allocated concatenation')
        segm_len = sum(ds_segms_len)
        sp_df = pd.DataFrame({
            'mz': np.zeros(segm_len, dtype=ds_segm_dtype),
            'int': np.zeros(segm_len, dtype=np.float32),
            'sp_i': np.zeros(segm_len, dtype=np.uint32),
        })
        row_start = 0
        for cobject in ds_segms_cobjects:
            sub_sp_df = read_ds_segment(cobject, hybrid_impl, storage)
            row_end = row_start + len(sub_sp_df)
            sp_df.iloc[row_start:row_end] = sub_sp_df
            row_start += len(sub_sp_df)

    else:
        with ThreadPoolExecutor(max_workers=128) as pool:
            sp_df = list(pool.map(lambda co: read_ds_segment(co, hybrid_impl, storage), ds_segms_cobjects))
        sp_df = pd.concat(sp_df, ignore_index=True, sort=False)

    return sp_df


def make_sample_area_mask(coordinates):
    pixel_indices = get_pixel_indices(coordinates)
    nrows, ncols = ds_dims(coordinates)
    sample_area_mask = np.zeros(ncols * nrows, dtype=bool)
    sample_area_mask[pixel_indices] = True
    return sample_area_mask.reshape(nrows, ncols)


def choose_ds_segments(ds_segments_bounds, centr_df, ppm):
    centr_segm_min_mz, centr_segm_max_mz = centr_df.mz.agg([np.min, np.max])
    centr_segm_min_mz -= centr_segm_min_mz * ppm * 1e-6
    centr_segm_max_mz += centr_segm_max_mz * ppm * 1e-6

    ds_segm_n = len(ds_segments_bounds)
    first_ds_segm_i = np.searchsorted(ds_segments_bounds[:, 0], centr_segm_min_mz, side='right') - 1
    first_ds_segm_i = max(0, first_ds_segm_i)
    last_ds_segm_i = np.searchsorted(ds_segments_bounds[:, 1], centr_segm_max_mz, side='left')  # last included
    last_ds_segm_i = min(ds_segm_n - 1, last_ds_segm_i)
    return first_ds_segm_i, last_ds_segm_i


def create_process_segment(ds_segms_cobjects, ds_segments_bounds, ds_segms_len,
                           imzml_reader, image_gen_config, pw_mem_mb, ds_segm_size_mb,
                           hybrid_impl):
    ds_segm_dtype = imzml_reader.mzPrecision
    sample_area_mask = make_sample_area_mask(imzml_reader.coordinates)
    nrows, ncols = ds_dims(imzml_reader.coordinates)
    compute_metrics = make_compute_image_metrics(sample_area_mask, nrows, ncols, image_gen_config)
    ppm = image_gen_config['ppm']

    def process_centr_segment(db_segm_cobject, id, storage):
        print(f'Reading centroids segment {id}')
        # read database relevant part
        centr_df = read_cloud_object_with_retry(storage, db_segm_cobject, deserialise)

        # find range of datasets
        first_ds_segm_i, last_ds_segm_i = choose_ds_segments(ds_segments_bounds, centr_df, ppm)
        print(f'Reading dataset segments {first_ds_segm_i}-{last_ds_segm_i}')
        # read all segments in loop from COS
        sp_arr = read_ds_segments(ds_segms_cobjects[first_ds_segm_i:last_ds_segm_i+1],
                                  ds_segms_len[first_ds_segm_i:last_ds_segm_i+1], pw_mem_mb,
                                  ds_segm_size_mb, ds_segm_dtype, hybrid_impl, storage)

        formula_images_it = gen_iso_images(
            sp_inds=sp_arr.sp_i.values, sp_mzs=sp_arr.mz.values, sp_ints=sp_arr.int.values,
            centr_df=centr_df, nrows=nrows, ncols=ncols, ppm=ppm, min_px=1
        )
        if hybrid_impl:
            safe_mb = pw_mem_mb // 2
        else:
            safe_mb = 1024
        max_formula_images_mb = (pw_mem_mb - safe_mb - (last_ds_segm_i - first_ds_segm_i + 1) * ds_segm_size_mb) // 3
        print(f'Max formula_images size: {max_formula_images_mb} mb')
        images_manager = ImagesManager(storage, max_formula_images_mb * 1024 ** 2)
        formula_image_metrics(formula_images_it, compute_metrics, images_manager)
        images_cloud_objs = images_manager.finish()

        print(f'Centroids segment {id} finished')
        formula_metrics_df = pd.DataFrame.from_dict(images_manager.formula_metrics, orient='index')
        formula_metrics_df.index.name = 'formula_i'
        return formula_metrics_df, images_cloud_objs

    return process_centr_segment


def to_png(img, mask):
    import png
    arr = img.toarray()
    arr = ((arr - arr.min()) / (arr.max() - arr.min())) * (2 ** 16 - 1)
    grey = np.empty(shape=img.shape + (2,), dtype=np.uint16)
    grey[:, :, 0] = arr.astype(np.uint16)
    grey[:, :, 1] = (mask * (2 ** 16 - 1)).astype(np.uint16)

    fp = BytesIO()
    png_writer = png.Writer(
        width=img.shape[1],
        height=img.shape[0],
        alpha=True,
        greyscale=True,
        bitdepth=16,
    )
    png_writer.write(fp, grey.reshape(grey.shape[0], -1))
    fp.seek(0)
    return fp.read()


def get_target_images(pw, images_cloud_objs, imzml_reader, targets, as_png=True, only_first_isotope=True):
    def get_target_images(images_cobject, storage):
        images = {}
        segm_images = read_cloud_object_with_retry(storage, images_cobject, deserialise)

        for k, imgs in segm_images.items():
            if k in targets:
                if only_first_isotope:
                    imgs = imgs[:1]
                if as_png:
                    imgs = [to_png(img, mask) if img is not None else None for img in imgs]
                images[k] = imgs
        return images

    mask = make_sample_area_mask(imzml_reader.coordinates)

    memory_capacity_mb = 1024
    futures = pw.map(
        get_target_images,
        [co for co in images_cloud_objs],
        runtime_memory=memory_capacity_mb,
    )

    all_images = {}
    for image_set in pw.get_result(futures):
        all_images.update(image_set)

    PipelineStats.append_func(futures, memory_mb=memory_capacity_mb)

    return all_images