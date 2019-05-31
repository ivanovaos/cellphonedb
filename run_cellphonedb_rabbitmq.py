#!/usr/bin/env python

import io
import json
import os
import sys
import tempfile
import time
import traceback

import boto3
import pandas as pd
import pika

from cellphonedb.src.app import cpdb_app
from cellphonedb.src.app.app_logger import app_logger
from cellphonedb.src.core.exceptions.AllCountsFilteredException import AllCountsFilteredException
from cellphonedb.src.core.exceptions.EmptyResultException import EmptyResultException
from cellphonedb.src.core.exceptions.ThresholdValueException import ThresholdValueException
from cellphonedb.src.core.utils.subsampler import Subsampler
from cellphonedb.src.exceptions.ParseCountsException import ParseCountsException
from cellphonedb.src.exceptions.ParseMetaException import ParseMetaException
from cellphonedb.src.exceptions.PlotException import PlotException
from cellphonedb.src.exceptions.ReadFileException import ReadFileException
from cellphonedb.src.plotters.r_plotter import dot_plot, heatmaps_plot
from cellphonedb.utils import utils

try:
    s3_access_key = os.environ['S3_ACCESS_KEY']
    s3_secret_key = os.environ['S3_SECRET_KEY']
    s3_bucket_name = os.environ['S3_BUCKET_NAME']
    s3_endpoint = os.environ['S3_ENDPOINT']
    rabbit_host = os.environ['RABBIT_HOST']
    rabbit_port = os.environ['RABBIT_PORT']
    rabbit_user = os.environ['RABBIT_USER']
    rabbit_password = os.environ['RABBIT_PASSWORD']
    jobs_queue_name = os.environ['RABBIT_JOB_QUEUE']
    result_queue_name = os.environ['RABBIT_RESULT_QUEUE']


except KeyError as e:
    app_logger.error('ENVIRONMENT VARIABLE {} not defined. Please set it'.format(e))
    exit(1)


def create_rabbit_connection():
    return pika.BlockingConnection(pika.ConnectionParameters(
        host=rabbit_host,
        port=rabbit_port,
        virtual_host='/',
        credentials=credentials
    ))


app = cpdb_app.create_app()

s3_resource = boto3.resource('s3', aws_access_key_id=s3_access_key,
                             aws_secret_access_key=s3_secret_key,
                             endpoint_url=s3_endpoint)

s3_client = boto3.client('s3', aws_access_key_id=s3_access_key,
                         aws_secret_access_key=s3_secret_key,
                         endpoint_url=s3_endpoint)


def read_data_from_s3(filename: str, s3_bucket_name: str, index_column_first: bool):
    s3_object = s3_client.get_object(Bucket=s3_bucket_name, Key=filename)
    return utils.read_data_from_s3_object(s3_object, filename, index_column_first=index_column_first)


def write_data_in_s3(data: pd.DataFrame, filename: str):
    result_buffer = io.StringIO()
    data.to_csv(result_buffer, index=False, sep='\t')
    result_buffer.seek(0)

    # TODO: Find more elegant solution (connexion closes after timeout)
    s3_client = boto3.client('s3', aws_access_key_id=s3_access_key,
                             aws_secret_access_key=s3_secret_key,
                             endpoint_url=s3_endpoint)

    s3_client.put_object(Body=result_buffer.getvalue().encode('utf-8'), Bucket=s3_bucket_name, Key=filename)


def write_image_to_s3(path: str, filename: str):
    _io = open(path, 'rb')

    # TODO: Find more elegant solution (connexion closes after timeout)
    s3_client = boto3.client('s3', aws_access_key_id=s3_access_key,
                             aws_secret_access_key=s3_secret_key,
                             endpoint_url=s3_endpoint)

    s3_client.put_object(Body=_io, Bucket=s3_bucket_name, Key=filename)


def dot_plot_results(means: str, pvalues: str, rows: str, columns: str, job_id: str):
    with tempfile.TemporaryDirectory() as output_path:
        with tempfile.NamedTemporaryFile(suffix=os.path.splitext(means)[-1]) as means_file:
            with tempfile.NamedTemporaryFile(suffix=os.path.splitext(pvalues)[-1]) as pvalues_file:
                with tempfile.NamedTemporaryFile() as rows_file:
                    with tempfile.NamedTemporaryFile() as columns_file:
                        _from_s3_to_temp(means, means_file)
                        _from_s3_to_temp(pvalues, pvalues_file)
                        _from_s3_to_temp(rows, rows_file)
                        _from_s3_to_temp(columns, columns_file)

                        output_name = 'plot__{}.png'.format(job_id)

                        dot_plot(means_file.name, pvalues_file.name, output_path, output_name, rows_file.name,
                                 columns_file.name)

                        output_file = os.path.join(output_path, output_name)

                        if not os.path.exists(output_file):
                            raise PlotException('Could not generate output file for plot of type dot_plot')

                        response = {
                            'job_id': job_id,
                            'files': {
                                'plot': output_name,
                            },
                            'success': True
                        }

                        write_image_to_s3(output_file, output_name)

                        return response


def heatmaps_plot_results(meta: str, pvalues: str, job_id: str):
    with tempfile.TemporaryDirectory() as output_path:
        with tempfile.NamedTemporaryFile(suffix=os.path.splitext(pvalues)[-1]) as pvalues_file:
            with tempfile.NamedTemporaryFile() as meta_file:
                _from_s3_to_temp(pvalues, pvalues_file)
                _from_s3_to_temp(meta, meta_file)

                count_name = 'plot_count__{}.png'.format(job_id)
                count_log_name = 'plot_count_log__{}.png'.format(job_id)

                heatmaps_plot(meta_file.name, pvalues_file.name, output_path, count_name, count_log_name)

                output_count_file = os.path.join(output_path, count_name)
                output_count_log_file = os.path.join(output_path, count_log_name)

                if not os.path.exists(output_count_file) or not os.path.exists(output_count_log_file):
                    raise PlotException('Could not generate output file for plot of type dot_plot')

                response = {
                    'job_id': job_id,
                    'files': {
                        'count_plot': count_name,
                        'count_log_plot': count_log_name
                    },
                    'success': True
                }

                write_image_to_s3(output_count_file, count_name)
                write_image_to_s3(output_count_log_file, count_log_name)

                return response


def _from_s3_to_temp(key, file):
    data = s3_client.get_object(Bucket=s3_bucket_name, Key=key)
    file.write(data['Body'].read())
    file.seek(0)

    return file


def process_plot(method, properties, body) -> dict:
    metadata = json.loads(body.decode('utf-8'))
    job_id = metadata['job_id']
    app_logger.info('New Plot Queued: {}'.format(job_id))

    plot_type = metadata.get('type', None)

    if plot_type == 'dot_plot':
        return dot_plot_results(metadata.get('file_means'),
                                metadata.get('file_pvalues'),
                                metadata.get('file_rows', None),
                                metadata.get('file_columns', None),
                                job_id
                                )

    if plot_type == 'heatmaps_plot':
        return heatmaps_plot_results(metadata.get('file_meta'),
                                     metadata.get('file_pvalues'),
                                     job_id
                                     )

    return {
        'job_id': job_id,
        'success': False,
        'error': {
            'id': 'UnknownPlotType',
            'message': 'Given plot type does not exist: {}'.format(plot_type)
        }
    }


def process_method(method, properties, body) -> dict:
    metadata = json.loads(body.decode('utf-8'))
    job_id = metadata['job_id']
    app_logger.info('New Job Queued: {}'.format(job_id))
    meta = read_data_from_s3(metadata['file_meta'], s3_bucket_name, index_column_first=False)
    counts = read_data_from_s3(metadata['file_counts'], s3_bucket_name, index_column_first=True)

    subsampler = Subsampler(bool(metadata['log']),
                            int(metadata['num_pc']),
                            int(metadata['num_cells']) if metadata.get('num_cells', False) else None
                            ) if metadata.get('subsampling', False) else None

    if metadata['iterations']:
        response = statistical_analysis(meta, counts, job_id, metadata, subsampler)
    else:
        response = non_statistical_analysis(meta, counts, job_id, metadata, subsampler)

    return response


def statistical_analysis(meta, counts, job_id, metadata, subsampler):
    pvalues, means, significant_means, deconvoluted = \
        app.method.cpdb_statistical_analysis_launcher(meta,
                                                      counts,
                                                      threshold=float(metadata['threshold']),
                                                      iterations=int(metadata['iterations']),
                                                      debug_seed=-1,
                                                      threads=4,
                                                      result_precision=int(metadata['result_precision']),
                                                      pvalue=float(metadata.get('pvalue', 0.05)),
                                                      subsampler=subsampler,
                                                      )
    response = {
        'job_id': job_id,
        'files': {
            'pvalues': 'pvalues_simple_{}.txt'.format(job_id),
            'means': 'means_simple_{}.txt'.format(job_id),
            'significant_means': 'significant_means_simple_{}.txt'.format(job_id),
            'deconvoluted': 'deconvoluted_simple_{}.txt'.format(job_id),
        },
        'success': True
    }
    write_data_in_s3(pvalues, response['files']['pvalues'])
    write_data_in_s3(means, response['files']['means'])
    write_data_in_s3(significant_means, response['files']['significant_means'])
    write_data_in_s3(deconvoluted, response['files']['deconvoluted'])
    return response


def non_statistical_analysis(meta, counts, job_id, metadata, subsampler):
    means, significant_means, deconvoluted = \
        app.method.cpdb_method_analysis_launcher(meta,
                                                 counts,
                                                 threshold=float(metadata['threshold']),
                                                 result_precision=int(metadata['result_precision']),
                                                 subsampler=subsampler,
                                                 )
    response = {
        'job_id': job_id,
        'files': {
            'means': 'means_simple_{}.txt'.format(job_id),
            'significant_means': 'significant_means_{}.txt'.format(job_id),
            'deconvoluted': 'deconvoluted_simple_{}.txt'.format(job_id),
        },
        'success': True
    }
    write_data_in_s3(means, response['files']['means'])
    write_data_in_s3(significant_means, response['files']['significant_means'])
    write_data_in_s3(deconvoluted, response['files']['deconvoluted'])
    return response


consume_more_jobs = True

credentials = pika.PlainCredentials(rabbit_user, rabbit_password)
connection = create_rabbit_connection()
channel = connection.channel()
channel.basic_qos(prefetch_count=1)

jobs_runned = 0

while jobs_runned < 3 and consume_more_jobs:
    job = channel.basic_get(queue=jobs_queue_name, no_ack=True)

    if all(job):
        try:
            if jobs_queue_name == 'plot_jobs':
                job_response = process_plot(*job)
            else:
                job_response = process_method(*job)

            # TODO: Find more elegant solution
            connection = create_rabbit_connection()
            channel = connection.channel()
            channel.basic_qos(prefetch_count=1)

            channel.basic_publish(exchange='', routing_key=result_queue_name, body=json.dumps(job_response))
            app_logger.info('JOB %s PROCESSED' % job_response['job_id'])
        except (ReadFileException, ParseMetaException, ParseCountsException, ThresholdValueException,
                AllCountsFilteredException, EmptyResultException, PlotException) as e:
            error_response = {
                'job_id': json.loads(job[2].decode('utf-8'))['job_id'],
                'success': False,
                'error': {
                    'id': str(e),
                    'message': (' {}.'.format(e.description) if hasattr(e, 'description') and e.description else '') +
                               (' {}.'.format(e.hint) if hasattr(e, 'hint') and e.hint else '')

                }
            }
            print(traceback.print_exc(file=sys.stdout))
            app_logger.error('[-] ERROR DURING PROCESSING JOB %s' % error_response['job_id'])
            if connection.is_closed:
                connection = create_rabbit_connection()
                channel = connection.channel()
                channel.basic_qos(prefetch_count=1)
            channel.basic_publish(exchange='', routing_key=result_queue_name, body=json.dumps(error_response))
            app_logger.error(e)
        except Exception as e:
            error_response = {
                'job_id': json.loads(job[2].decode('utf-8'))['job_id'],
                'success': False,
                'error': {
                    'id': 'unknown_error',
                    'message': ''
                }
            }
            print(traceback.print_exc(file=sys.stdout))
            app_logger.error('[-] ERROR DURING PROCESSING JOB %s' % error_response['job_id'])
            if connection.is_closed:
                connection = create_rabbit_connection()
                channel = connection.channel()
                channel.basic_qos(prefetch_count=1)
            channel.basic_publish(exchange='', routing_key=result_queue_name, body=json.dumps(error_response))
            app_logger.error(e)

        jobs_runned += 1

    else:
        app_logger.debug('Empty queue')

    time.sleep(1)
