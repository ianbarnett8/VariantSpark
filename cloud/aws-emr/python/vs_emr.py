#!/usr/bin/env python

import sys
import os
import yaml
import click
import pystache
from pystache import Renderer
import json
import subprocess
import jsonmerge
import functools
import shlex
from pkg_resources import resource_filename, resource_string

EMR_TEMPL = "aws emr add-steps --cluster-id %(cluster_id)s --steps Type=Spark,Name='%(step_name)s',ActionOnFailure=%(action_on_failure)s,Args=[%(arg_list)s]"
VS_EMR_ARGS = ['--class','au.csiro.variantspark.cli.VariantSparkApp','/mnt/variant-spark/variant-spark_2.11-all.jar']

class AWSContext(object):
    def __init__(self, noop = False, verbose = False, silent = False):
        self.noop = noop
        self.verbose = verbose
        self.silent = silent
    
    def aws_emr_step(self, cluster_id, step_name, action_on_failure, args):
        output =  self.aws_run(EMR_TEMPL % dict(cluster_id = cluster_id, step_name = step_name,
                    action_on_failure = action_on_failure , arg_list = ",".join(args)))
        return output and json.loads(output)['StepIds'][0] 
    
    def echo(self, msg):
        if not self.silent:
            click.echo(msg)
            
    def debug(self, msg):
        if self.verbose:
            self.echo(msg)
            
    def aws_run(self, cmd):
        if (self.noop):
            click.echo("Noop! Cmd is: %s" % cmd)
        else:
            self.debug("Running: %s" % cmd)
            output =  subprocess.check_output(cmd, shell=True)
            self.debug("Output: %s" % output)
            return output
        
    def load_config(self, config_file):
        is_default_config = config_file is None
        config_file = config_file if config_file is not None else os.path.join(os.environ['HOME'], '.vs_emr/config.yaml')
        
        configuration = {}
        
        if os.path.exists(config_file):
            self.echo("Loading config from: %s" % config_file)
            configuration = load_yaml(config_file)
        elif is_default_config:
            echo("Default config file not fount at: %s. Running with empty configuration!" % config_file )
        else:
            raise(click.BadParameter("Provided config file does not exists: %s" % config_file))
        self.debug("Configuration is: %s" % configuration)
        return Configuration(configuration)
            
pass_aws_cxt = click.make_pass_decorator(AWSContext)
           
def resolve_cluster_id(aws_ctx, cluster_id, cluster_id_file):
    if cluster_id is None:
        if cluster_id_file is not None:
            aws_ctx.echo("Loading cluster info from: %s" % cluster_id_file)
            with open(cluster_id_file, "r") as input:
                cluster_info = json.load(input)
            aws_ctx.debug("Cluster info is: %s" % str(cluster_info))
            cluster_id = cluster_info['ClusterId']    
        else:
            raise click.BadOptionUsage('--cluster-id or --cluster-id-file is required')                
    return cluster_id
                
'''
'''
def dict_put_path(dictionary, path_key, value):
    path = path_key.split(".")
    current_dict = dictionary
    for key in path[:-1]:
        sub_dict = current_dict.get(key)
        if dict != type(sub_dict):
            sub_dict = dict()
            current_dict[key] = sub_dict
        current_dict = sub_dict
    current_dict[path[-1]] = value


def dict_put(dictionary, pv ):
    path_key, value = pv
    dict_put_path(dictionary, path_key, value)
    return dictionary

def resolve_to_cmd_options(aws_ctx, template_file, user_config):
    
    def to_cmd_option(k,v):
        if "tags" == k:
            return "--%s %s" % (k," ".join("'%s=%s'" % kv for kv in v.items()))            
        elif bool == type(v):
            return ("--%s" if v else "--no-%s") % k
        elif list == type(v) or dict == type(v):
            return "--%s '%s'" % (k,json.dumps(v))
        else:
            return "--%s %s" % (k,json.dumps(v))
        
    with open(template_file, 'r') as template_f:
        template  = template_f.read()
        
        
    unresolved_config = yaml.load(pystache.render(template, {})) 
    unresolved_defaults = unresolved_config.get('defaults') or dict()
    defaults = yaml.load(pystache.render(template, jsonmerge.merge(unresolved_defaults, user_config))).get('defaults') or dict()
    config = jsonmerge.merge(defaults, user_config)
    aws_config = yaml.load(pystache.render(template, config))   
    aws_ctx.debug("AWS Config: %s" % aws_config)    
    aws_options = aws_config['options']
    cmd_options = [to_cmd_option(*kv) for kv in aws_options.items()]
    if aws_ctx.verbose:
        aws_ctx.debug("AWS-Options:")
        for opt in cmd_options:
            aws_ctx.debug(opt)    
    
    return cmd_options  


def load_yaml(conf_file):
    with open(conf_file, "r") as cf:
        return yaml.load(cf)  


def cmd_conf_to_config(conf):
    def split_conf_string(s):
        index = s.find('=')
        print(index)
        return (s[0:index],s[index+1:])  
    config = dict()
    for conf_entry in conf:
        key, value = split_conf_string(conf_entry)
        dict_put_path(config, key, value)        
    return config
    
    
def merge_configs(configs):
    return functools.reduce(jsonmerge.merge,[dict()] + configs)  
    
def resolve_config(conf_file, conf_json, conf):
           
    def split_conf_string(s):
        index = s.find('=')
        print(index)
        return (s[0:index],s[index+1:])    

    conf_dict = dict()
    for conf_entry in conf:
        key, value = split_conf_string(conf_entry)
        dict_put_path(conf_dict, key, value)        
    return functools.reduce(jsonmerge.merge,[dict()] + [load_yaml(conf_file_item) for conf_file_item in conf_file] +  [ json.loads(conf_json_item) for conf_json_item in conf_json] + [conf_dict])  


class Configuration:
    
    def __init__(self, configuration):
        self.configuration = configuration
        
    def resolve_config(self, profiles, options, conf):
        
        def load_profile(profile_name):
            conf_profile = conf_profiles.get(profile_name)
            if conf_profile is not None:
                return conf_profile
            else:
                raise click.BadParameter("Profile `%s` not defined in the configuration" % profile_name)

        conf_defaults = self.configuration.get('default')
        conf_profiles = self.configuration.get('profiles', dict())
        profile_configs = [load_profile(profile_name) for profile_name in  profiles] if conf_profiles else []
        config = merge_configs([conf_defaults]+ profile_configs + [ cmd_conf_to_config(conf),  options])
        return config

#
# Command line interface
#

@click.group()
#this needs to be moved to vnl.sumbmit.main somehow but for now I just to not have any idea how do it
@click.option('--dry-run', help='Dry run. Do not execute the actual command', is_flag=True)
@click.option('--verbose', help='Produce verbose output', is_flag=True)
@click.option('--silent', help='Do not produce any output', is_flag=True)
@click.pass_context
def cli(ctx, dry_run, verbose, silent):
    ctx.obj = AWSContext(dry_run, verbose, silent)


MAP_OPTIONS_TO_CONFIG = dict(
    worker_type="worker.instanceType",
    worker_instances="worker.instanceCount",
    worker_bid="worker.bidPrice",
    master_type="master.instanceType",
    master_bid="master.bidPrice"
)

@cli.command(name='start-cluster')
@click.option('--worker-type', required = False, help='The type of AWS EC2 instance to use for worker nodes. E.g. r4.2xlarge')
@click.option('--worker-instances', required = False, help='The number of worker instances in the cluster')
@click.option('--worker-bid', required = False, help='The maximum spot price for the worker instances')
@click.option('--master-type', required = False)
@click.option('--master-bid', required = False)
@click.option('--profile',  multiple=True)
@click.option('--conf',  multiple=True)
@click.option('--cluster-id-file',  required = False)
@click.option('--config-file', required = False)
@pass_aws_cxt
def start_cluster(aws_ctx, conf, profile, cluster_id_file, config_file, **kwargs):
    
    def options_to_conf():        
        return functools.reduce(dict_put, ((MAP_OPTIONS_TO_CONFIG[k], v) for k,v in  kwargs.items() if v is not None), dict())
    
    configuration  = aws_ctx.load_config(config_file)
    if profile:
        aws_ctx.echo("Using profiles: %s" % list(profile))
    config = configuration.resolve_config(profile,options_to_conf(), conf)
    cmd_options = resolve_to_cmd_options(aws_ctx, resource_filename(__name__, os.path.join('templates','spot-cluster.yaml')), config)
    cmd = " ".join(['aws', 'emr', 'create-cluster'] + cmd_options)
    output = aws_ctx.aws_run(cmd)
    if not aws_ctx.noop:
        if cluster_id_file is not None:
            aws_ctx.echo("Saving cluster info to: %s" % cluster_id_file)
            with open(cluster_id_file, "w") as output_file:
                output_file.write(output)
        aws_ctx.echo(output)
        
@cli.command(name='stop-cluster')
@click.option("--cluster-id", required = False)
@click.option("--cluster-id-file", required = False)
@pass_aws_cxt
def kill_cluster(aws_ctx, cluster_id, cluster_id_file):
    cluster_id = resolve_cluster_id(aws_ctx, cluster_id, cluster_id_file)    
    aws_ctx.echo("Stopping cluster with id: %s" % cluster_id)
    cmd = " ".join(['aws', 'emr', 'terminate-clusters', '--cluster-id', cluster_id])
    output = aws_ctx.aws_run(cmd) 
    aws_ctx.echo(output)
    # add waiting for termination aws emr wait cluster-running/cluster-terminated --cluster-id j-3SD91U2E1L2QX
    
    
@cli.command(name='submit-cmd', context_settings=dict(
    ignore_unknown_options=True,
))
@click.option("--cluster-id", required = False)
@click.option("--cluster-id-file", required = False)
@click.option("--step-name", required = False, default="variant-spark")
@click.option("--action_on_failure", required=False, default="CONTINUE", 
              type=click.Choice(['CONTINUE', 'TERMINATE_CLUSTER', 'CANCEL_AND_WAIT']))
@click.option("--spark-opts", required=False)
@click.argument('variant_spark_args', nargs=-1, type=click.UNPROCESSED)
@pass_aws_cxt
def submit_cmd(aws_ctx, cluster_id, cluster_id_file, step_name, action_on_failure, spark_opts, variant_spark_args):
    cluster_id = resolve_cluster_id(aws_ctx, cluster_id, cluster_id_file)     
    aws_ctx.echo("At cluster: %s running: %s" %  (cluster_id, " ".join(variant_spark_args)))
    step_id = aws_ctx.aws_emr_step(cluster_id, step_name, action_on_failure, shlex.split(spark_opts or '') + VS_EMR_ARGS + list(variant_spark_args))
    aws_ctx.echo("Step Id: %s" % step_id)


if __name__ == '__main__':
    cli()
