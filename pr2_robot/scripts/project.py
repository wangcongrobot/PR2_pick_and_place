#!/usr/bin/env python

# Import modules
import numpy as np
import sklearn
from sklearn.preprocessing import LabelEncoder
import pickle
from sensor_stick.srv import GetNormals
from sensor_stick.features import compute_color_histograms
from sensor_stick.features import compute_normal_histograms
from visualization_msgs.msg import Marker
from sensor_stick.marker_tools import *
from sensor_stick.msg import DetectedObjectsArray
from sensor_stick.msg import DetectedObject
from sensor_stick.pcl_helper import *

import rospy
import tf
from geometry_msgs.msg import Pose
from std_msgs.msg import Float64
from std_msgs.msg import Int32
from std_msgs.msg import String
from pr2_robot.srv import *
from rospy_message_converter import message_converter
import yaml
import os

from std_srvs.srv import Empty
from math import pi
from sensor_msgs.msg import JointState

# Helper function to get surface normals
def get_normals(cloud):
    get_normals_prox = rospy.ServiceProxy('/feature_extractor/get_normals', GetNormals)
    return get_normals_prox(cloud).cluster

# Helper function to create a yaml friendly dictionary from ROS messages
def make_yaml_dict(test_scene_num, arm_name, object_name, pick_pose, place_pose):
    yaml_dict = {}
    yaml_dict["test_scene_num"] = test_scene_num.data
    yaml_dict["arm_name"]  = arm_name.data
    yaml_dict["object_name"] = object_name.data
    yaml_dict["pick_pose"] = message_converter.convert_ros_message_to_dictionary(pick_pose)
    yaml_dict["place_pose"] = message_converter.convert_ros_message_to_dictionary(place_pose)
    return yaml_dict

# Helper function to output to yaml file
def send_to_yaml(yaml_filename, dict_list):
    data_dict = {"object_list": dict_list}
    with open(yaml_filename, 'w') as outfile:
        yaml.dump(data_dict, outfile, default_flow_style=False)

def save_pcd(cloud, filename):
    if not os.path.isfile(filename):
        pcl.save(cloud, filename)
        
# Callback function for your Point Cloud Subscriber
def pcl_callback(pcl_msg):
    global table_cluster
   
    # Convert ROS msg to PCL data
    cloud = ros_to_pcl(pcl_msg)
    save_pcd(cloud, 'original.pcd')
    
    # Statistical Outlier Filtering
    outlier_filter = cloud.make_statistical_outlier_filter()
    outlier_filter.set_mean_k(25)
    outlier_filter.set_std_dev_mul_thresh(1)
    cloud_no_outliers = outlier_filter.filter()

    rospy.loginfo("Completed outlier filtering. Points in cloud: {}".format(cloud_no_outliers.size))
    save_pcd(cloud_no_outliers, 'no_outlier.pcd')
    
    # Voxel Grid Downsampling
    vox = cloud_no_outliers.make_voxel_grid_filter()
    # voxel size (also called leafs)
    LEAF_SIZE = 0.01
    vox.set_leaf_size(LEAF_SIZE, LEAF_SIZE, LEAF_SIZE)
    cloud_downsampled = vox.filter()

    rospy.loginfo("Completed downsampling. Points in cloud: {}".format(cloud_downsampled))
    save_pcd(cloud_downsampled, 'downsampled.pcd')
    
    # PassThrough Filter
    passthrough_z = cloud_downsampled.make_passthrough_filter()
    filter_axis = 'z'
    passthrough_z.set_filter_field_name(filter_axis)
    axis_min = 0.60
    axis_max = 4
    passthrough_z.set_filter_limits(axis_min, axis_max)
    cloud_filtered = passthrough_z.filter()

    rospy.loginfo("Completed passthrough. Points in cloud: {}".format(cloud_filtered))
    save_pcd(cloud_filtered, 'passthrough.pcd')
    
    # TODO: RANSAC Plane Segmentation
    seg = cloud_filtered.make_segmenter()
    seg.set_model_type(pcl.SACMODEL_PLANE)
    seg.set_method_type(pcl.SAC_RANSAC)    
    max_distance = 0.01
    seg.set_distance_threshold(max_distance)    
    inliers, coefficients = seg.segment()

    # Extract inliers and outliers
    cloud_table = cloud_filtered.extract(inliers, negative=False)
    cloud_objects_pre = cloud_filtered.extract(inliers, negative=True)

    passthrough_y = cloud_objects_pre.make_passthrough_filter()
    filter_axis = 'y'
    passthrough_y.set_filter_field_name(filter_axis)
    axis_min = -0.5
    axis_max = 0.5
    passthrough_y.set_filter_limits(axis_min, axis_max)
    cloud_objects = passthrough_y.filter()
    
    rospy.loginfo("Completed ransac")
    save_pcd(cloud_table, 'table.pcd')
    save_pcd(cloud_objects_pre, 'objects_pre.pcd')
    save_pcd(cloud_objects, 'objects.pcd')
    
    # Euclidean Clustering
    white_cloud = XYZRGB_to_XYZ(cloud_objects)
    kd_tree = white_cloud.make_kdtree()
    ec = white_cloud.make_EuclideanClusterExtraction()
    ec.set_ClusterTolerance(0.03)
    ec.set_MinClusterSize(50)
    ec.set_MaxClusterSize(2200)
    
    ec.set_SearchMethod(kd_tree)
    cluster_indices = ec.Extract()

    # Create Cluster-Mask Point Cloud to visualize each cluster separately
    cluster_color = get_color_list(len(cluster_indices))
    color_cluster_point_list = []
    for j, indices in enumerate(cluster_indices):
        for i, indice in enumerate(indices):
            color_cluster_point_list.append([white_cloud[indice][0], white_cloud[indice][1], white_cloud[indice][2], rgb_to_float(cluster_color[j])])

    # Create new cloud containing all clusters, each with unique color
    cluster_cloud = pcl.PointCloud_PointXYZRGB()
    cluster_cloud.from_list(color_cluster_point_list)
            
    # Convert PCL data to ROS messages
    ros_cloud_table = pcl_to_ros(cloud_table)
    ros_cloud_objects = pcl_to_ros(cloud_objects)
    ros_cluster_cloud = pcl_to_ros(cluster_cloud)
    
    rospy.loginfo("Publishing results")
    
    # Publish ROS messages
    pcl_objects_pub.publish(ros_cloud_objects)
    pcl_table_pub.publish(ros_cloud_table)
    pcl_cluster_pub.publish(ros_cluster_cloud)

    rospy.loginfo("Completed euclidean clustering")
    save_pcd(cluster_cloud, 'cluster_cloud.pcd')

    # Classify the clusters! (loop through each detected cluster one at a time)
    detected_objects_labels = []
    detected_objects = []

    for index, pts_list in enumerate(cluster_indices):
        # Grab the points for the cluster from the extracted outliers (cloud_objects)
        pcl_cluster = cloud_objects.extract(pts_list)
        ros_cluster = pcl_to_ros(pcl_cluster)

        # Compute the associated feature vector
        chists = compute_color_histograms(ros_cluster, using_hsv=True)
        normals = get_normals(ros_cluster)
        nhists = compute_normal_histograms(normals)
        feature = np.concatenate((chists, nhists))
        
        # Make the prediction
        prediction = clf.predict(scaler.transform(feature.reshape(1,-1)))
        label = encoder.inverse_transform(prediction)[0]
        detected_objects_labels.append(label)
        
        # Publish a label into RViz
        label_pos = list(white_cloud[pts_list[0]])
        label_pos[2] += .4
        object_markers_pub.publish(make_label(label,label_pos, index))
        
        # Add the detected object to the list of detected objects.
        do = DetectedObject()
        do.label = label
        do.cloud = ros_cluster
        detected_objects.append(do)

    rospy.loginfo('Detected {} objects: {}'.format(len(detected_objects_labels), detected_objects_labels))

    # Publish the list of detected objects
    detected_objects_pub.publish(detected_objects)
    
    # Suggested location for where to invoke your pr2_mover() function within pcl_callback()
    # Could add some logic to determine whether or not your object detections are robust
    # before calling pr2_mover()

    # setup table cloud
    # we keep appending newly discovered table points to the table points we have already found
    table_cluster = table_cluster + cloud_table.to_list()
    pcl_table_cluster = pcl.PointCloud_PointXYZRGB()
    pcl_table_cluster.from_list(table_cluster)
    clear_collision_map()
    collision_map_pub.publish(pcl_to_ros(pcl_table_cluster))
    
    try:
       pr2_mover(detected_objects, table_cluster)
    except rospy.ROSInterruptException:
       pass

# function to load parameters and request PickPlace service
def pr2_mover(object_list, table_point_cloud):
    
    if not is_turning_done:
        return
    
    # Initialize variables
    # the dict_cloud_by_label and dict_centroid_by_label are mappings of point_cloud and centroid indexed label
    # these are used to fetch the data for a detected object
    labels = []
    centroids = []
    dict_centroid_by_label = {}
    dict_cloud_by_label = {}
    for object in object_list:
        labels.append(object.label)
        points = ros_to_pcl(object.cloud)
        points_arr = points.to_array()
        # Get the PointCloud for a given object and obtain it's centroid
        centroid = np.mean(points_arr, axis=0)[:3]
        centroids.append(centroid)
        dict_centroid_by_label[object.label] = centroid
        dict_cloud_by_label[object.label] = points

    # Get/Read parameters for which objects are to be picked up and where the boxes are
    object_list_param = rospy.get_param('/object_list')
    dropbox_param = rospy.get_param('/dropbox')

    # Parse parameters into individual variable
    dropbox_left = dropbox_param[0]['position']
    dropbox_right = dropbox_param[1]['position']
    
    dict_list = []
    # TODO: Loop through the pick list
    for i in range(0, len(object_list_param)):

        rospy.loginfo("picking up {}".format(i))
        
        # TODO: Create 'place_pose' for the object
        test_scene_num = Int32()
        test_scene_num.data = 3

        object_name = String()
        object_name.data = object_list_param[i]['name']

        if object_name.data not in dict_centroid_by_label:
            rospy.loginfo("Couldnt find {}".format(object_name.data))
            continue

        del dict_cloud_by_label[object_name.data]
            
        pick_pose = Pose()
        pick_pose.position.x = np.asscalar(dict_centroid_by_label[object_name.data][0])
        pick_pose.position.y = np.asscalar(dict_centroid_by_label[object_name.data][1])
        pick_pose.position.z = np.asscalar(dict_centroid_by_label[object_name.data][2])

        # TODO: Assign the arm to be used for pick_place
        arm_name = String()
        arm_name.data = "left" if object_list_param[i]['group'] == "red" else "right"

        place_pose = Pose()
        dropbox_target = dropbox_left if arm_name.data == "left" else dropbox_right
        place_pose.position.x = dropbox_target[0]
        place_pose.position.y = dropbox_target[1]
        place_pose.position.z = dropbox_target[2]
        
        # TODO: Create a list of dictionaries (made with make_yaml_dict()) for later output to yaml format
        yaml_dict = make_yaml_dict(test_scene_num, arm_name, object_name, pick_pose, place_pose)
        #print(yaml_dict)
        dict_list.append(yaml_dict)

        rospy.loginfo("Before wait for pick place routine")
        # Wait for 'pick_place_routine' service to come up
        rospy.wait_for_service('pick_place_routine')
        rospy.loginfo("Pick place routine started")

        # Update the collision map (first clear it)
        clear_collision_map()
        collision_points = table_point_cloud
        for (key, points) in dict_cloud_by_label.items():
            collision_points = collision_points + points.to_list()
        collision_cloud = pcl.PointCloud_PointXYZRGB()
        collision_cloud.from_list(collision_points)
        collision_map_pub.publish(pcl_to_ros(collision_cloud))
        rospy.loginfo("collision map published")
        
        try:
            pick_place_routine = rospy.ServiceProxy('pick_place_routine', PickPlace)

            # TODO: Insert your message variables to be sent as a service request
            resp = pick_place_routine(test_scene_num, object_name, arm_name, pick_pose, place_pose)

            print ("Response: ",resp.success)

        except rospy.ServiceException, e:
            print "Service call failed: %s"%e

    # TODO: Output your request parameters into output yaml file
    send_to_yaml("result.yaml", dict_list)


def jointCheck(jointState):
    global expect_idx, next_move, expect_world, is_turning_done
    
    curWorldState = jointState.position[-1]
    #rospy.loginfo("checking with {} and value {} and {}".format(expect_idx, curWorldState, expect_world[expect_idx]))
    if abs(curWorldState - expect_world[expect_idx]) < 1e-4:
        nextMov = next_move[expect_idx]
        rospy.loginfo("updating to next state {}".format(nextMov))
        if (nextMov == -100):
            joint_sub.unregister()
            is_turning_done = True
        else:
            robot_hip_joint.publish(nextMov)
            expect_idx = expect_idx+1 

def clear_collision_map():
    rospy.wait_for_service("/clear_octomap")
    collision_map_clearer = rospy.ServiceProxy('/clear_octomap', Empty)
    try:
        collision_map_clearer()
    except rospy.ServiceException as exc: 
        rospy.loginfo("failed with {}".format(str(exc)))

if __name__ == '__main__':
    # TODO: ROS node initialization
    rospy.init_node("clustering", anonymous = True)

    #Load model from disk
    model = pickle.load(open("model.sav", "rb"))
    clf = model["classifier"]
    encoder = LabelEncoder()
    encoder.classes_ = model["classes"]
    scaler = model["scaler"]
    
    # Initialize variables
    get_color_list.color_list = []
    table_cluster = []
    rospy.loginfo("Variables setup completed")

    # collision map clearer
    
    # initialize scene perception callback
    pcl_sub = rospy.Subscriber("/pr2/world/points", pc2.PointCloud2, pcl_callback, queue_size = 1)

    # TODO: Create Publishers
    pcl_objects_pub = rospy.Publisher("/pcl_objects", pc2.PointCloud2, queue_size = 1)
    pcl_table_pub = rospy.Publisher("/pcl_table", pc2.PointCloud2, queue_size = 1)
    pcl_cluster_pub = rospy.Publisher("/pcl_cluster", pc2.PointCloud2, queue_size = 1)

    object_markers_pub = rospy.Publisher("/object_markers", Marker, queue_size = 1)
    detected_objects_pub = rospy.Publisher("/detected_objects", DetectedObjectsArray, queue_size = 1)

    collision_map_pub = rospy.Publisher("/pr2/3d_map/points", pc2.PointCloud2, queue_size = 1)
    robot_hip_joint = rospy.Publisher("/pr2/world_joint_controller/command", Float64, queue_size = 10)
        
    rospy.loginfo("Clusering node setup completed")
        
    rate = rospy.Rate(2)
    rate.sleep()

    is_turning_done = False
    expect_world = [0, pi/2, -pi/2, 0]
    next_move = [pi/2, -pi/2, 0, -100]
    expect_idx = 0
    # Rotate PR2 in place to capture side tables for the collision map
    # initialize turning with scene perception callback already in place
    joint_sub = rospy.Subscriber("/pr2/joint_states", JointState, jointCheck, queue_size = 1)

    busy_wait = rospy.Rate(1)
    while not is_turning_done:
        busy_wait.sleep()

    rospy.loginfo("turning is done")
        
    # TODO: Spin while node is not shutdown
    while not rospy.is_shutdown():
        rospy.spin()
