#!/usr/bin/env python
# -*- coding: UTF-8 -*-

# Add parent path so we can import modules
import sys
sys.path.insert(0,'..')

from argparse import ArgumentParser
from collections import OrderedDict
import faiss
import glob
import numpy as np
import os
import pandas as pd
import pickle
from sklearn.metrics.pairwise import cosine_similarity
import shutil
import time

import torch
from torch.utils.data import DataLoader

from .data_processing import test_tokenize
from .rnn_networks import test_model
from .utils import read_input_file
from .utils import read_command_candidate_ranker
from .utils_candidate_ranker import query_vector_gen
from .utils_candidate_ranker import candidate_conf_calc
# --- set seed for reproducibility
from .utils import set_seed_everywhere
set_seed_everywhere(1364)

# skip future warnings for now XXX
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

# ------------------- candidate_ranker --------------------
def candidate_ranker(input_file_path="default", query_scenario=None, candidate_scenario=None,
                     ranking_metric="faiss", selection_threshold=0.8, 
                     query=None, num_candidates=10, search_size=4, output_path="ranker_output",
                     pretrained_model_path=None, pretrained_vocab_path=None, number_test_rows=-1):

    start_time = time.time()
    
    if input_file_path in ["default"]:
        found_input = False
        detect_input_files = glob.iglob(os.path.join(candidate_scenario, "*.yaml"))
        for detected_inp in detect_input_files:
            if os.path.isfile(detected_inp):
                input_file_path = detected_inp
                found_input = True
                break
        if not found_input:
            sys.exit(f"[ERROR] no input file (*.yaml file) could be found in the dir: {scenario}")
    
    # read input file
    dl_inputs = read_input_file(input_file_path)

    if (ranking_metric.lower() in ["faiss"]) and (selection_threshold < 0):
        sys.exit(f"[ERROR] Threshold for the selected metric: '{ranking_metric}' should be >= 0.")
    if (ranking_metric.lower() in ["cosine", "conf"]) and not (0 <= selection_threshold <= 1):
        sys.exit(f"[ERROR] Threshold for the selected metric: '{ranking_metric}' should be between 0 and 1.")
    
    if not ranking_metric.lower() in ["faiss", "cosine", "conf"]:
        sys.exit(f"[ERROR] ranking_metric of {ranking_metric.lower()} is not supported. "\
                  "Current ranking methods are: 'faiss', 'cosine', 'conf'")
    
    # ----- CANDIDATES
    path1_combined = os.path.join(candidate_scenario, "fwd.pt")
    path2_combined = os.path.join(candidate_scenario, "bwd.pt")
    path_id_combined = os.path.join(candidate_scenario, "fwd_id.pt")
    path_items_combined = os.path.join(candidate_scenario, "fwd_items.npy")
    
    vecs_ids_candidates = torch.load(path_id_combined, map_location=dl_inputs['general']['device'])
    vecs_items_candidates = np.load(path_items_combined, allow_pickle=True)
    vecs1_candidates = torch.load(path1_combined, map_location=dl_inputs['general']['device'])
    vecs2_candidates = torch.load(path2_combined, map_location=dl_inputs['general']['device'])
    vecs_candidates = torch.cat([vecs1_candidates, vecs2_candidates], dim=1)    
            
    if (not pretrained_model_path in [False, None]) or query:
        # --- load torch model, send it to the device (CPU/GPU)
        model = torch.load(pretrained_model_path, map_location=dl_inputs['general']['device'])
        # --- create test data class
        # read vocabulary
        with open(pretrained_vocab_path, 'rb') as handle:
            train_vocab = pickle.load(handle)

    # ----- QUERIES
    if query:
        tmp_dirname = query_vector_gen(query, model, train_vocab, dl_inputs)
        query_scenario = os.path.join(tmp_dirname, "combined", "query_on_fly")
        mydf = pd.read_pickle(os.path.join(tmp_dirname, "query", "dataframe.df"))
        vecs_items = mydf[['s1_unicode', "s1"]].to_numpy()
        np.save(os.path.join(tmp_dirname, "fwd_items.npy"), vecs_items)
        path_items_combined = os.path.join(tmp_dirname, "fwd_items.npy")
    else:
        path_items_combined = os.path.join(query_scenario, "fwd_items.npy")

    path1_combined = os.path.join(query_scenario, f"fwd.pt")
    path2_combined = os.path.join(query_scenario, f"bwd.pt")
    path_id_combined = os.path.join(query_scenario, f"fwd_id.pt")
    
    vecs_ids_query = torch.load(path_id_combined, map_location=dl_inputs['general']['device'])
    vecs_items_query = np.load(path_items_combined, allow_pickle=True)
    vecs1_query = torch.load(path1_combined, map_location=dl_inputs['general']['device'])
    vecs2_query = torch.load(path2_combined, map_location=dl_inputs['general']['device'])
    vecs_query = torch.cat([vecs1_query, vecs2_query], dim=1)

    if query:
        shutil.rmtree(tmp_dirname)

    if (number_test_rows > 0) and (number_test_rows < len(vecs_query)):
        len_vecs_query = number_test_rows
    else:
        len_vecs_query = len(vecs_query)

    # --- start FAISS
    faiss_id_candis = faiss.IndexFlatL2(vecs_candidates.size()[1])   # build the index
    print("Is faiss_id_candis already trained? %s" % faiss_id_candis.is_trained)
    faiss_id_candis.add(vecs_candidates.detach().cpu().numpy())

    # Empty dataframe to collect data
    output_pd = pd.DataFrame()
    for iq in range(len_vecs_query):
        print("=========== Start the search for %s" % iq, vecs_items_query[iq][1])
        collect_neigh_pd = pd.DataFrame()
        num_found_candidates = 0
        # start with 0:seach_size
        # If the number of selected candidates < num_candidates
        # Increase the search size
        id_0_neigh = 0
        id_1_neigh = search_size
        while (num_found_candidates < num_candidates):
            if id_1_neigh > len(vecs_candidates):
                id_1_neigh = len(vecs_candidates)
            if id_0_neigh == id_1_neigh:
                break
    
            found_neighbours = faiss_id_candis.search(vecs_query[iq:(iq+1)].detach().cpu().numpy(), id_1_neigh)
        
            # Candidates
            orig_id_candis = found_neighbours[1][0, id_0_neigh:id_1_neigh]
            all_candidates = vecs_items_candidates[orig_id_candis][:, 0]
            all_candidates_orig = vecs_items_candidates[orig_id_candis][:, 1]
        
            # Queries
            orig_id_queries = vecs_ids_query[iq].item()
            all_queries = [vecs_items_query[orig_id_queries][0]]*(id_1_neigh - id_0_neigh)
            all_queries_no_preproc = [vecs_items_query[orig_id_queries][1]]*(id_1_neigh - id_0_neigh)
    
            query_candidate_pd = pd.DataFrame(all_queries, columns=['s1'])
            query_candidate_pd['s2'] = all_candidates
            query_candidate_pd['s2_orig'] = all_candidates_orig
            query_candidate_pd['label'] = "False"
    
            # Compute cosine similarity
            cosine_sim = cosine_similarity(vecs_query[iq:(iq+1)].detach().cpu().numpy(), 
                                           vecs_candidates.detach().cpu().numpy()[orig_id_candis])
    
            if not pretrained_model_path in [False, None]:
                all_preds = candidate_conf_calc(query_candidate_pd, 
                                                model, 
                                                train_vocab, 
                                                dl_inputs, 
                                                cutoffs=(id_1_neigh - id_0_neigh))
                query_candidate_pd['dl_match'] = all_preds.detach().cpu().numpy()
    
            else:
                query_candidate_pd['dl_match'] = [None]*len(query_candidate_pd)
    
    
            query_candidate_pd['faiss_dist'] = found_neighbours[0][0, id_0_neigh:id_1_neigh]
            query_candidate_pd['cosine_sim'] = cosine_sim[0] 
            query_candidate_pd['s1_orig_ids'] = orig_id_queries 
            query_candidate_pd['s2_orig_ids'] = orig_id_candis 
    
            if ranking_metric.lower() in ["faiss"]:
                query_candidate_filtered_pd = query_candidate_pd[query_candidate_pd["faiss_dist"] <= selection_threshold]
            elif ranking_metric.lower() in ["cosine"]:
                query_candidate_filtered_pd = query_candidate_pd[query_candidate_pd["cosine_sim"] >= selection_threshold]
            elif ranking_metric.lower() in ["conf"]:
                if not pretrained_model_path in [False, None]:
                    query_candidate_filtered_pd = query_candidate_pd[query_candidate_pd["dl_match"] >= selection_threshold]
                else:
                    sys.exit(f"ranking_metric: {ranking_metric} is selected, but --model_path is not specified.")
            else:
                sys.exit(f"[ERROR] ranking_metric: {ranking_metric} is not implemented. See the documentation.")
    
            num_found_candidates += len(query_candidate_filtered_pd)
            print("ID: %s/%s -- Number of found candidates so far: %s, searched: %s" % (iq+1, len(vecs_query), num_found_candidates, id_1_neigh))
    
            if num_found_candidates > 0:
                collect_neigh_pd = collect_neigh_pd.append(query_candidate_filtered_pd)
            
            if ranking_metric.lower() in ["faiss"]:
                # 1.01 is multiplied to avoid issues with float numbers and rounding erros
                if query_candidate_pd["faiss_dist"].max() > (selection_threshold*1.01):
                    break
            elif ranking_metric.lower() in ["cosine"]:
                # 0.99 is multiplied to avoid issues with float numbers and rounding errors
                if query_candidate_pd["cosine_sim"].min() < (selection_threshold*0.99):
                    break 
    
            # Go to the next zone    
            if (num_found_candidates < num_candidates):
                id_0_neigh, id_1_neigh = id_1_neigh, id_1_neigh + search_size
    
        
        # write results to output_pd
        mydict_dl_match = OrderedDict({})
        mydict_faiss_dist = OrderedDict({})
        mydict_candid_id = OrderedDict({})
        mydict_cosine_sim = OrderedDict({})
        if len(collect_neigh_pd) == 0:
            one_row = {
                "id": orig_id_queries, 
                "query": all_queries_no_preproc[0], 
                "pred_score": [mydict_dl_match], 
                "faiss_distance": [mydict_faiss_dist], 
                "cosine_sim": [mydict_cosine_sim],
                "candidate_original_ids": [mydict_candid_id], 
                "query_original_id": orig_id_queries,
                "num_all_searches": id_1_neigh 
                }
            output_pd = output_pd.append(pd.DataFrame.from_dict(one_row))
            continue
        if ranking_metric.lower() in ["faiss"]:
            collect_neigh_pd = collect_neigh_pd.sort_values(by="faiss_dist")[:num_candidates]
        elif ranking_metric.lower() in ["cosine"]:
            collect_neigh_pd = collect_neigh_pd.sort_values(by="cosine_sim", ascending=False)[:num_candidates]
        elif ranking_metric.lower() in ["conf"]:
            collect_neigh_pd = collect_neigh_pd.sort_values(by="dl_match", ascending=False)[:num_candidates]
    
        for i_row, row in collect_neigh_pd.iterrows():
            if not pretrained_model_path in [False, None]:
                mydict_dl_match[row["s2_orig"]] = round(row["dl_match"], 4)
            else:
                mydict_dl_match[row["s2_orig"]] = row["dl_match"]
            mydict_faiss_dist[row["s2_orig"]] = round(row["faiss_dist"], 4)
            mydict_cosine_sim[row["s2_orig"]] = round(row["cosine_sim"], 4)
            mydict_candid_id[row["s2_orig"]] = row["s2_orig_ids"]
        one_row = {
            "id": orig_id_queries, 
            "query": all_queries_no_preproc[0], 
            "pred_score": [mydict_dl_match], 
            "faiss_distance": [mydict_faiss_dist], 
            "cosine_sim": [mydict_cosine_sim],
            "candidate_original_ids": [mydict_candid_id], 
            "query_original_id": orig_id_queries,
            "num_all_searches": id_1_neigh 
            }
        output_pd = output_pd.append(pd.DataFrame.from_dict(one_row))
           
    if len(output_pd) == 0:
        return None
    output_pd = output_pd.set_index("id")
    output_path = os.path.abspath(output_path)
    if not os.path.isdir(os.path.dirname(output_path)):
        os.makedirs(os.path.dirname(output_path))
    output_pd.to_pickle(os.path.join(f"{output_path}.pkl"))
    elapsed = time.time() - start_time
    print("TOTAL TIME: %s" % elapsed)
    return output_pd

def main():
    # --- read args from the command line
    input_file_path, query_scenario, candidate_scenario, ranking_metric, selection_threshold,\
        query, num_candidates, search_size, output_path, pretrained_model_path, pretrained_vocab_path, number_test_rows = \
        read_command_candidate_ranker()
    
    # --- 
    candidate_ranker(input_file_path=input_file_path, 
                     query_scenario=query_scenario, 
                     candidate_scenario=candidate_scenario,
                     ranking_metric=ranking_metric, 
                     selection_threshold=selection_threshold, 
                     query=query,
                     num_candidates=num_candidates, 
                     search_size=search_size, 
                     output_path=output_path,
                     pretrained_model_path=pretrained_model_path, 
                     pretrained_vocab_path=pretrained_vocab_path, 
                     number_test_rows=number_test_rows)

if __name__ == '__main__':
    main()
